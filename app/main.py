import asyncio
import hashlib
import hmac
import os
import re
import subprocess
import tarfile
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.middleware.sessions import SessionMiddleware

from . import backup as backup_mod, db, endpoints as endpoints_mod, mgmt, netlimit, notify, poller, settings, system

APP_VERSION = "1.0.0"
REPO_URL = "https://github.com/CodeScaffold/ovpn-dashboard"

EASYRSA_DIR = Path("/etc/openvpn/easy-rsa")
SERVER_CRL = Path("/etc/openvpn/server/crl.pem")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

VPN_ENDPOINT = os.environ["VPN_ENDPOINT"]
SESSION_SECRET = os.environ["SESSION_SECRET"]

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
RESERVED_NAMES = {"server"}
ALLOWED_CIPHERS = {"AES-128-GCM", "AES-256-GCM", "CHACHA20-POLY1305"}

# ---- brute-force backoff (in-memory, single-process, fine for a single-admin box) ----
_failed_attempts: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _is_locked_out(ip: str, cfg: dict) -> bool:
    attempts = _failed_attempts.get(ip, [])
    cutoff = time.time() - cfg["lockout_seconds"]
    attempts = [t for t in attempts if t > cutoff]
    _failed_attempts[ip] = attempts
    return len(attempts) >= cfg["lockout_max_attempts"]


def _record_failure(ip: str) -> None:
    _failed_attempts.setdefault(ip, []).append(time.time())


def _clear_failures(ip: str) -> None:
    _failed_attempts.pop(ip, None)


def verify_password(password: str) -> bool:
    cfg = settings.load()
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(cfg["salt"]), 200_000)
    return hmac.compare_digest(candidate, bytes.fromhex(cfg["hash"]))


def require_auth(request: Request) -> None:
    sess = request.session
    if not sess.get("auth"):
        raise HTTPException(status_code=401, detail="Not authenticated")
    cfg = settings.load()
    if sess.get("epoch") != cfg["session_epoch"]:
        sess.clear()
        raise HTTPException(status_code=401, detail="Session was signed out remotely.")
    timeout_min = cfg["session_timeout_minutes"]
    if timeout_min:
        last = sess.get("last_active", 0)
        if time.time() - last > timeout_min * 60:
            sess.clear()
            raise HTTPException(status_code=401, detail="Session timed out.")
    sess["last_active"] = time.time()


def run_easyrsa(*args: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "EASYRSA_BATCH": "1", **(extra_env or {})}
    result = subprocess.run(
        ["./easyrsa", *args], cwd=EASYRSA_DIR, env=env, capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"easyrsa {' '.join(args)} failed: {result.stderr.strip()[-800:]}")
    return result


def read_client_index() -> dict[str, dict]:
    """Parses easy-rsa's pki/index.txt once: {name: {"status": "V"|"R", "expires": iso|None}}."""
    index = EASYRSA_DIR / "pki" / "index.txt"
    if not index.exists():
        return {}
    out: dict[str, dict] = {}
    for line in index.read_text().splitlines():
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        status, expiry_raw = parts[0], parts[1]
        m = re.search(r"/CN=([^/]+)", parts[5])
        if not m:
            continue
        cn = m.group(1)
        if cn in RESERVED_NAMES:
            continue
        expires_iso = None
        try:
            expires_iso = datetime.strptime(expiry_raw, "%y%m%d%H%M%SZ").replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass
        out[cn] = {"status": status, "expires": expires_iso}
    return out


def read_valid_client_names() -> list[str]:
    return [name for name, info in read_client_index().items() if info["status"] == "V"]


def client_created_at(name: str) -> str:
    cert = EASYRSA_DIR / "pki" / "issued" / f"{name}.crt"
    if not cert.exists():
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(cert.stat().st_mtime, tz=timezone.utc).isoformat()


def build_ovpn(name: str, endpoint_name: str) -> str:
    ep = db.get_endpoint(endpoint_name)
    if not ep:
        raise HTTPException(status_code=404, detail=f'No endpoint named "{endpoint_name}".')
    pki = EASYRSA_DIR / "pki"
    ca = (pki / "ca.crt").read_text()
    cert_raw = (pki / "issued" / f"{name}.crt").read_text()
    cert = cert_raw[cert_raw.index("-----BEGIN CERTIFICATE-----"):]
    key = (pki / "private" / f"{name}.key").read_text()
    tls_crypt = (pki / "tls-crypt.key").read_text()

    return f"""client
dev tun
proto {ep['protocol']}
remote {VPN_ENDPOINT} {ep['port']}
resolv-retry infinite
nobind
persist-key
persist-tun
remote-cert-tls server
cipher {ep['cipher']}
data-ciphers {ep['cipher']}
auth SHA256
verb 3
<ca>
{ca.strip()}
</ca>
<cert>
{cert.strip()}
</cert>
<key>
{key.strip()}
</key>
<tls-crypt>
{tls_crypt.strip()}
</tls-crypt>
"""


def refresh_crl() -> None:
    run_easyrsa("gen-crl")
    crl_src = EASYRSA_DIR / "pki" / "crl.pem"
    SERVER_CRL.write_text(crl_src.read_text())
    SERVER_CRL.chmod(0o644)


def client_view(name: str, expires: str | None = None) -> dict:
    db.ensure_client_row(name)
    row = db.get_limits(name)
    if expires is None:
        expires = read_client_index().get(name, {}).get("expires")
    return {
        "name": name,
        "created": client_created_at(name),
        "expires": expires,
        "max_connections": row["max_connections"],
        "quota_bytes": row["quota_bytes"],
        "used_bytes": row["used_bytes"],
        "bandwidth_kbps": row["bandwidth_kbps"],
        "disabled": bool(row["disabled"]),
    }


def apply_limits(name: str, max_connections: int, quota_gb: float | None, bandwidth_mbps: float | None) -> None:
    quota_bytes = int(quota_gb * 1_000_000_000) if quota_gb else None
    bandwidth_kbps = int(bandwidth_mbps * 1000) if bandwidth_mbps else None

    db.ensure_client_row(name)
    row = db.get_limits(name)
    all_endpoints = db.get_all_endpoints()
    client_ips = db.get_client_ips(name)

    try:
        if bandwidth_kbps:
            for ep in all_endpoints:
                ip = client_ips.get(ep["name"])
                if not ip:
                    used = db.get_endpoint_used_ips(ep["name"])
                    ip = db.allocate_static_ip(ep["subnet"], used)
                    db.set_client_ip(name, ep["name"], ip)
                netlimit.apply_bandwidth(ep["tun_dev"], ip, bandwidth_kbps)
        else:
            for ep in all_endpoints:
                ip = client_ips.get(ep["name"])
                if ip:
                    netlimit.remove_bandwidth(ep["tun_dev"], ip)
            db.clear_client_ips(name)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=f"Failed to apply bandwidth limit: {e}")

    db.update_limits(name, max_connections=max_connections, quota_bytes=quota_bytes, bandwidth_kbps=bandwidth_kbps)

    client_ips = db.get_client_ips(name)
    for ep in all_endpoints:
        netlimit.write_ccd(ep["ccd_dir"], name, client_ips.get(ep["name"]), disabled=bool(row["disabled"]))


def migrate_main_endpoint() -> None:
    """One-time bootstrap: register the already-running hand-provisioned OpenVPN
    instance as the 'main' endpoint, and carry over any pre-multi-endpoint static IP."""
    db.ensure_endpoint(
        name="server", port=1194, protocol="udp", cipher="AES-128-GCM",
        mgmt_port=7505, tun_dev="tun0", subnet="10.9.0.0/24",
        dns1="1.1.1.1", dns2="1.0.0.1", ccd_dir="/etc/openvpn/ccd",
    )
    conn = db.get_conn()
    rows = conn.execute("SELECT name, static_ip FROM client_limits WHERE static_ip IS NOT NULL").fetchall()
    for r in rows:
        db.set_client_ip(r["name"], "server", r["static_ip"])


app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, max_age=60 * 60 * 12, same_site="lax")


@app.on_event("startup")
async def on_startup():
    db.get_conn()
    migrate_main_endpoint()
    system.set_retention(settings.load()["retention_points"])
    asyncio.create_task(poller.run_forever())


class LoginBody(BaseModel):
    password: str


class CreateClientBody(BaseModel):
    name: str
    max_connections: int = 1
    quota_gb: float | None = None
    bandwidth_mbps: float | None = None
    validity_days: int | None = None  # None = fall back to the saved client-default template


class LimitsBody(BaseModel):
    max_connections: int = 1
    quota_gb: float | None = None
    bandwidth_mbps: float | None = None


class RenewBody(BaseModel):
    validity_days: int


class CreateEndpointBody(BaseModel):
    name: str
    port: int
    protocol: str
    cipher: str
    dns1: str = ""
    dns2: str = ""


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str


class SecuritySettingsBody(BaseModel):
    session_timeout_minutes: int
    lockout_max_attempts: int
    lockout_seconds: int


class NotificationsSettingsBody(BaseModel):
    notify_webhook_enabled: bool
    notify_webhook_url: str = ""
    notify_telegram_enabled: bool
    notify_telegram_bot_token: str = ""
    notify_telegram_chat_id: str = ""


class ClientDefaultsBody(BaseModel):
    max_connections: int
    quota_gb: float | None = None
    bandwidth_mbps: float | None = None
    validity_days: int = 825


class RetentionBody(BaseModel):
    points: int


@app.post("/api/login")
def login(body: LoginBody, request: Request):
    ip = _client_ip(request)
    cfg = settings.load()
    if _is_locked_out(ip, cfg):
        raise HTTPException(status_code=429, detail="Too many attempts. Try again in a few minutes.")
    if not verify_password(body.password):
        _record_failure(ip)
        raise HTTPException(status_code=401, detail="Incorrect password.")
    _clear_failures(ip)
    request.session["auth"] = True
    request.session["epoch"] = cfg["session_epoch"]
    request.session["last_active"] = time.time()
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.put("/api/settings/password")
def change_password(body: ChangePasswordBody, request: Request):
    require_auth(request)
    if not verify_password(body.current_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters.")
    salt = secrets.token_bytes(16)
    new_hash = hashlib.pbkdf2_hmac("sha256", body.new_password.encode(), salt, 200_000)
    settings.update({"hash": new_hash.hex(), "salt": salt.hex()})
    return {"ok": True}


@app.get("/api/settings")
def get_settings(request: Request):
    require_auth(request)
    cfg = settings.load()
    return {
        "session_timeout_minutes": cfg["session_timeout_minutes"],
        "lockout_max_attempts": cfg["lockout_max_attempts"],
        "lockout_seconds": cfg["lockout_seconds"],
        "notify_webhook_enabled": cfg["notify_webhook_enabled"],
        "notify_webhook_url": cfg["notify_webhook_url"],
        "notify_telegram_enabled": cfg["notify_telegram_enabled"],
        "notify_telegram_bot_token": cfg["notify_telegram_bot_token"],
        "notify_telegram_chat_id": cfg["notify_telegram_chat_id"],
        "client_default_max_connections": cfg["client_default_max_connections"],
        "client_default_quota_gb": cfg["client_default_quota_gb"],
        "client_default_bandwidth_mbps": cfg["client_default_bandwidth_mbps"],
        "client_default_validity_days": cfg["client_default_validity_days"],
        "retention_points": cfg["retention_points"],
        "panel_version": APP_VERSION,
        "panel_repo": REPO_URL,
    }


@app.put("/api/settings/security")
def update_security_settings(body: SecuritySettingsBody, request: Request):
    require_auth(request)
    if not (0 <= body.session_timeout_minutes <= 10080):
        raise HTTPException(status_code=400, detail="Session timeout must be 0 (disabled) to 10080 minutes (7 days).")
    if not (2 <= body.lockout_max_attempts <= 20):
        raise HTTPException(status_code=400, detail="Lockout threshold must be between 2 and 20 attempts.")
    if not (30 <= body.lockout_seconds <= 86400):
        raise HTTPException(status_code=400, detail="Lockout duration must be between 30 seconds and 24 hours.")
    settings.update({
        "session_timeout_minutes": body.session_timeout_minutes,
        "lockout_max_attempts": body.lockout_max_attempts,
        "lockout_seconds": body.lockout_seconds,
    })
    return {"ok": True}


@app.post("/api/settings/logout-others")
def logout_other_sessions(request: Request):
    require_auth(request)
    cfg = settings.load()
    new_epoch = cfg["session_epoch"] + 1
    settings.update({"session_epoch": new_epoch})
    request.session["epoch"] = new_epoch  # keep the current tab logged in
    return {"ok": True}


@app.put("/api/settings/notifications")
def update_notifications_settings(body: NotificationsSettingsBody, request: Request):
    require_auth(request)
    settings.update({
        "notify_webhook_enabled": body.notify_webhook_enabled,
        "notify_webhook_url": body.notify_webhook_url.strip(),
        "notify_telegram_enabled": body.notify_telegram_enabled,
        "notify_telegram_bot_token": body.notify_telegram_bot_token.strip(),
        "notify_telegram_chat_id": body.notify_telegram_chat_id.strip(),
    })
    return {"ok": True}


@app.post("/api/settings/notifications/test")
def test_notifications(request: Request):
    require_auth(request)
    cfg = settings.load()
    if not cfg["notify_webhook_enabled"] and not cfg["notify_telegram_enabled"]:
        raise HTTPException(status_code=400, detail="Enable and save at least one notification channel first.")
    notify.notify(cfg, "test", "This is a test notification from your OpenVPN Admin panel.")
    return {"ok": True}


@app.put("/api/settings/client-defaults")
def update_client_defaults(body: ClientDefaultsBody, request: Request):
    require_auth(request)
    if not (1 <= body.max_connections <= 10):
        raise HTTPException(status_code=400, detail="max_connections must be between 1 and 10.")
    if not (30 <= body.validity_days <= 3650):
        raise HTTPException(status_code=400, detail="Certificate validity must be between 30 and 3650 days.")
    settings.update({
        "client_default_max_connections": body.max_connections,
        "client_default_quota_gb": body.quota_gb,
        "client_default_bandwidth_mbps": body.bandwidth_mbps,
        "client_default_validity_days": body.validity_days,
    })
    return {"ok": True}


@app.put("/api/settings/retention")
def update_retention(body: RetentionBody, request: Request):
    require_auth(request)
    if not (20 <= body.points <= 1440):
        raise HTTPException(status_code=400, detail="Retention must be between 20 and 1440 samples.")
    settings.update({"retention_points": body.points})
    system.set_retention(body.points)
    return {"ok": True}


@app.get("/api/backup")
def download_backup(request: Request):
    require_auth(request)
    tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
    tmp.close()
    dest = Path(tmp.name)
    try:
        backup_mod.build_backup_tar(dest)
    except Exception as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Backup failed: {e}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return FileResponse(
        dest, media_type="application/gzip",
        filename=f"ovpn-dashboard-backup-{stamp}.tar.gz",
        background=BackgroundTask(dest.unlink, missing_ok=True),
    )


@app.post("/api/backup/restore")
async def restore_backup(request: Request, file: UploadFile):
    require_auth(request)
    if not file.filename.endswith((".tar.gz", ".tgz")):
        raise HTTPException(status_code=400, detail="Expected a .tar.gz backup file.")

    tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
    try:
        content = await file.read()
        tmp.write(content)
        tmp.close()

        # safety net: snapshot current state before touching anything
        backup_mod.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        try:
            backup_mod.build_backup_tar(backup_mod.BACKUP_DIR / f"pre-restore-{stamp}.tar.gz")
        except Exception:
            pass  # best-effort; must not block a restore the admin explicitly requested

        try:
            touched = backup_mod.restore_from_tar(Path(tmp.name))
        except (ValueError, tarfile.TarError) as e:
            raise HTTPException(status_code=400, detail=f"Restore failed: {e}")
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    # the sqlite connection + in-memory caches are only consistent for a freshly
    # started process, so exit non-zero and let systemd (Restart=on-failure) bring
    # a clean process back up. Delay lets this response actually reach the client.
    threading.Timer(1.5, lambda: os._exit(1)).start()
    return {"ok": True, "endpoints_restored": touched}


@app.get("/api/clients")
def list_clients(request: Request):
    require_auth(request)
    index = read_client_index()
    valid = sorted(n for n, info in index.items() if info["status"] == "V")
    return [client_view(n, index[n]["expires"]) for n in valid]


@app.post("/api/clients", status_code=201)
def create_client(body: CreateClientBody, request: Request):
    require_auth(request)
    name = body.name.strip()
    if not NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Use only letters, numbers, - and _ (max 32 chars).")
    if name.lower() in RESERVED_NAMES:
        raise HTTPException(status_code=400, detail="That name is reserved.")
    if name in read_valid_client_names():
        raise HTTPException(status_code=409, detail="A client with that name already exists.")
    if (EASYRSA_DIR / "pki" / "reqs" / f"{name}.req").exists():
        raise HTTPException(status_code=409, detail="That name was used before (even if revoked). Pick another.")
    if not (1 <= body.max_connections <= 10):
        raise HTTPException(status_code=400, detail="max_connections must be between 1 and 10.")

    cfg = settings.load()
    validity_days = body.validity_days or cfg["client_default_validity_days"]
    if not (30 <= validity_days <= 3650):
        raise HTTPException(status_code=400, detail="Certificate validity must be between 30 and 3650 days.")

    try:
        run_easyrsa("gen-req", name, "nopass")
        run_easyrsa("sign-req", "client", name, extra_env={"EASYRSA_CERT_EXPIRE": str(validity_days)})
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    db.ensure_client_row(name)
    if body.max_connections != 1 or body.quota_gb or body.bandwidth_mbps:
        apply_limits(name, body.max_connections, body.quota_gb, body.bandwidth_mbps)

    return client_view(name)


@app.get("/api/clients/{name}/download")
def download_client(name: str, request: Request, endpoint: str = "server"):
    require_auth(request)
    if name not in read_valid_client_names():
        raise HTTPException(status_code=404, detail="Client not found.")
    ovpn = build_ovpn(name, endpoint)
    return Response(
        content=ovpn,
        media_type="application/x-openvpn-profile",
        headers={"Content-Disposition": f'attachment; filename="{name}-{endpoint}.ovpn"'},
    )


@app.delete("/api/clients/{name}")
def revoke_client(name: str, request: Request):
    require_auth(request)
    if name.lower() in RESERVED_NAMES:
        raise HTTPException(status_code=400, detail="Refusing to revoke the server cert.")
    if name not in read_valid_client_names():
        raise HTTPException(status_code=404, detail="Client not found.")

    try:
        run_easyrsa("revoke", name)
        refresh_crl()
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    client_ips = db.get_client_ips(name)
    for ep in db.get_all_endpoints():
        ip = client_ips.get(ep["name"])
        if ip:
            try:
                netlimit.remove_bandwidth(ep["tun_dev"], ip)
            except RuntimeError:
                pass
        netlimit.remove_ccd(ep["ccd_dir"], name)
    db.delete_client(name)

    return {"ok": True}


@app.post("/api/clients/{name}/renew")
def renew_client(name: str, body: RenewBody, request: Request):
    require_auth(request)
    if name not in read_valid_client_names():
        raise HTTPException(status_code=404, detail="Client not found.")
    if not (30 <= body.validity_days <= 3650):
        raise HTTPException(status_code=400, detail="Certificate validity must be between 30 and 3650 days.")

    # Same-name reissue: revoke moves the old req/cert/key out of pki/reqs, pki/issued,
    # pki/private (into revoked/*_by_serial), so gen-req can freely recreate them under
    # the same CN. Limits, quota usage, ccd and bandwidth rules are untouched — this is
    # a cert swap, not a new client.
    try:
        run_easyrsa("revoke", name)
        refresh_crl()
        run_easyrsa("gen-req", name, "nopass")
        run_easyrsa("sign-req", "client", name, extra_env={"EASYRSA_CERT_EXPIRE": str(body.validity_days)})
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return client_view(name)


@app.put("/api/clients/{name}/limits")
def set_client_limits(name: str, body: LimitsBody, request: Request):
    require_auth(request)
    if name not in read_valid_client_names():
        raise HTTPException(status_code=404, detail="Client not found.")
    if not (1 <= body.max_connections <= 10):
        raise HTTPException(status_code=400, detail="max_connections must be between 1 and 10.")
    if body.quota_gb is not None and body.quota_gb <= 0:
        raise HTTPException(status_code=400, detail="quota_gb must be positive, or omitted for unlimited.")
    if body.bandwidth_mbps is not None and body.bandwidth_mbps <= 0:
        raise HTTPException(status_code=400, detail="bandwidth_mbps must be positive, or omitted for unlimited.")

    apply_limits(name, body.max_connections, body.quota_gb, body.bandwidth_mbps)
    return client_view(name)


@app.post("/api/clients/{name}/reset-usage")
def reset_client_usage(name: str, request: Request):
    require_auth(request)
    if name not in read_valid_client_names():
        raise HTTPException(status_code=404, detail="Client not found.")
    db.reset_usage(name)
    client_ips = db.get_client_ips(name)
    for ep in db.get_all_endpoints():
        netlimit.write_ccd(ep["ccd_dir"], name, client_ips.get(ep["name"]), disabled=False)
    return client_view(name)


@app.get("/api/status")
def api_status(request: Request):
    require_auth(request)
    all_endpoints = db.get_all_endpoints()
    sessions = mgmt.get_all_sessions(all_endpoints)
    return [
        {
            "name": s.name, "real_address": s.real_address, "virtual_address": s.virtual_address,
            "bytes_received": s.bytes_received, "bytes_sent": s.bytes_sent,
            "connected_since": s.connected_since, "client_id": s.client_id, "endpoint": s.endpoint,
        }
        for s in sessions
    ]


def openvpn_uptime_seconds() -> float:
    result = subprocess.run(
        ["systemctl", "show", "-p", "ActiveEnterTimestamp", "--value", "openvpn-server@server"],
        capture_output=True, text=True,
    )
    ts = result.stdout.strip()
    if not ts:
        return 0.0
    try:
        started = datetime.strptime(ts, "%a %Y-%m-%d %H:%M:%S %Z").replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
    except ValueError:
        return 0.0


@app.get("/api/system")
def api_system(request: Request):
    require_auth(request)
    snap = system.snapshot()
    snap["openvpn_uptime_seconds"] = openvpn_uptime_seconds()
    snap["vpn_endpoint"] = VPN_ENDPOINT
    return snap


def endpoint_view(ep) -> dict:
    online = 0
    try:
        online = len(mgmt.get_sessions(ep["mgmt_port"]))
    except OSError:
        pass
    return {
        "name": ep["name"], "port": ep["port"], "protocol": ep["protocol"], "cipher": ep["cipher"],
        "dns1": ep["dns1"], "dns2": ep["dns2"], "active": endpoints_mod.is_active(ep["name"]),
        "online": online, "created": ep["created"],
    }


@app.get("/api/endpoints")
def list_endpoints(request: Request):
    require_auth(request)
    return [endpoint_view(ep) for ep in db.get_all_endpoints()]


@app.post("/api/endpoints", status_code=201)
def create_endpoint(body: CreateEndpointBody, request: Request):
    require_auth(request)
    try:
        endpoints_mod.create_endpoint(body.name, body.port, body.protocol, body.cipher, body.dns1, body.dns2)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # any client with a bandwidth cap needs a static IP + tc rule on the new endpoint too
    for name, row in db.get_all_limits().items():
        if row["bandwidth_kbps"]:
            apply_limits(name, row["max_connections"], row["quota_bytes"] / 1_000_000_000 if row["quota_bytes"] else None,
                         row["bandwidth_kbps"] / 1000)

    return endpoint_view(db.get_endpoint(body.name))


@app.delete("/api/endpoints/{name}")
def delete_endpoint(name: str, request: Request):
    require_auth(request)
    try:
        endpoints_mod.delete_endpoint(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.post("/api/endpoints/{name}/restart")
def restart_endpoint(name: str, request: Request):
    require_auth(request)
    if name != "server":
        raise HTTPException(status_code=400, detail="Only the 'server' endpoint supports restart from here.")
    result = subprocess.run(
        ["sudo", "-n", "systemctl", "restart", "openvpn-server@server"], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=f"Restart failed: {result.stderr.strip()[-300:]}")
    notify.notify(settings.load(), "endpoint_restart", f'Endpoint "{name}" was restarted by the admin.')
    return {"ok": True}


@app.post("/api/endpoints/{name}/stop")
def stop_endpoint(name: str, request: Request):
    require_auth(request)
    if not db.get_endpoint(name):
        raise HTTPException(status_code=404, detail="Endpoint not found.")
    result = subprocess.run(
        ["sudo", "-n", "systemctl", "disable", "--now", f"openvpn-server@{name}"], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=f"Stop failed: {result.stderr.strip()[-300:]}")
    notify.notify(settings.load(), "endpoint_stop", f'Endpoint "{name}" was stopped by the admin.')
    return {"ok": True}


@app.post("/api/endpoints/{name}/start")
def start_endpoint(name: str, request: Request):
    require_auth(request)
    if not db.get_endpoint(name):
        raise HTTPException(status_code=404, detail="Endpoint not found.")
    result = subprocess.run(
        ["sudo", "-n", "systemctl", "enable", "--now", f"openvpn-server@{name}"], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=f"Start failed: {result.stderr.strip()[-300:]}")
    notify.notify(settings.load(), "endpoint_start", f'Endpoint "{name}" was started by the admin.')
    return {"ok": True}


@app.get("/api/endpoints/{name}/logs")
def endpoint_logs(name: str, request: Request):
    require_auth(request)
    if not db.get_endpoint(name):
        raise HTTPException(status_code=404, detail="Endpoint not found.")
    result = subprocess.run(
        ["journalctl", "-u", f"openvpn-server@{name}", "-n", "200", "--no-pager", "-o", "short-iso"],
        capture_output=True, text=True,
    )
    return {"logs": result.stdout or result.stderr}


@app.get("/api/endpoints/{name}/config")
def endpoint_config(name: str, request: Request):
    require_auth(request)
    if not db.get_endpoint(name):
        raise HTTPException(status_code=404, detail="Endpoint not found.")
    conf_path = Path("/etc/openvpn/server") / f"{name}.conf"
    if not conf_path.exists():
        raise HTTPException(status_code=404, detail="Config file not found.")
    return {"config": conf_path.read_text()}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.exception_handler(404)
async def spa_fallback(request: Request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=404, content={"detail": "Not found"})
    return FileResponse(STATIC_DIR / "index.html")
