import asyncio
import base64
import csv
import hashlib
import hmac
import io
import os
import re
import secrets
import sqlite3
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, UploadFile, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.middleware.sessions import SessionMiddleware

from . import backup as backup_mod, hub, hub_db, notify, settings
from .hub import RemoteOpError

APP_VERSION = "2.1.3"
REPO_URL = "https://github.com/CodeScaffold/ovpn-dashboard"

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

SESSION_SECRET = os.environ["SESSION_SECRET"]

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

ERROR_STATUS = {"not_found": 404, "conflict": 409, "bad_request": 400, "internal": 500}

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


def _require_server(server_id: int):
    row = hub_db.get_server(server_id)
    if not row:
        raise HTTPException(status_code=404, detail="Server not found.")
    return row


async def _call(server_id: int, op: str, data: dict | None = None) -> dict:
    """Calls an op on server_id's agent, translating transport/agent failures
    into the right HTTP status. This is the one place that bridges the
    hub<->agent RPC layer to the REST API."""
    try:
        return await hub.hub.call(server_id, op, data)
    except ConnectionError:
        raise HTTPException(status_code=503, detail="Server is not currently connected.")
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Server did not respond in time.")
    except RemoteOpError as e:
        raise HTTPException(status_code=ERROR_STATUS.get(e.code, 400), detail=str(e))


def _csv_rows(text: str) -> csv.DictReader:
    """Numbers/Excel export CSV with a locale-dependent delimiter -- e.g. Numbers on a
    machine set to a comma-decimal locale writes ';' instead of ','. Sniff it instead of
    assuming ',', or every row silently fails to parse (the whole line becomes one
    unrecognized column) with no visible error."""
    try:
        dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=",;\t")
    except (csv.Error, IndexError):
        dialect = csv.excel
    return csv.DictReader(io.StringIO(text), dialect=dialect)


app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, max_age=60 * 60 * 12, same_site="lax")


@app.on_event("startup")
async def on_startup():
    hub_db.get_conn()


class LoginBody(BaseModel):
    password: str


class CreateClientBody(BaseModel):
    name: str
    max_connections: int = 1
    quota_gb: float | None = None
    bandwidth_mbps: float | None = None
    validity_days: int | None = None  # None = fall back to the saved client-default template
    team: str | None = None
    label: str | None = None


class LimitsBody(BaseModel):
    max_connections: int = 1
    quota_gb: float | None = None
    bandwidth_mbps: float | None = None
    team: str | None = None
    label: str | None = None


class SetStaticIpBody(BaseModel):
    endpoint: str
    static_ip: str | None = None  # None/empty clears this endpoint's assignment


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


class CreateServerBody(BaseModel):
    name: str
    label: str = ""
    vpn_endpoint: str  # address this server's clients dial — the old single global VPN_ENDPOINT, now per-server


class UpdateServerBody(BaseModel):
    name: str
    label: str = ""
    vpn_endpoint: str


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
    return {"ok": True}


# ---------------- servers (hub-level: registry, enrollment, agent link) ----------------

@app.get("/api/servers")
def list_servers(request: Request):
    require_auth(request)
    rows = hub_db.list_servers()
    return [
        {
            "id": r["id"], "name": r["name"], "label": r["label"],
            "online": hub.hub.is_online(r["id"]), "agent_version": r["agent_version"],
            "hostname": r["hostname"], "public_addr": r["public_addr"], "vpn_endpoint": r["vpn_endpoint"],
            "last_seen": r["last_seen"], "created": r["created"],
        }
        for r in rows
    ]


@app.post("/api/servers", status_code=201)
def create_server(body: CreateServerBody, request: Request):
    require_auth(request)
    name = body.name.strip()
    if not NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Use only letters, numbers, - and _ (max 32 chars).")
    vpn_endpoint = body.vpn_endpoint.strip()
    if not vpn_endpoint:
        raise HTTPException(status_code=400, detail="vpn_endpoint is required — the address this server's clients will dial.")
    server_id, token = hub.generate_enrollment(name, vpn_endpoint, body.label.strip())
    # install_token is only ever returned here, at creation time — only its hash is stored.
    return {"id": server_id, "name": name, "install_token": token}


@app.put("/api/servers/{server_id}")
def update_server(server_id: int, body: UpdateServerBody, request: Request):
    require_auth(request)
    _require_server(server_id)
    name = body.name.strip()
    if not NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Use only letters, numbers, - and _ (max 32 chars).")
    vpn_endpoint = body.vpn_endpoint.strip()
    if not vpn_endpoint:
        raise HTTPException(status_code=400, detail="vpn_endpoint is required.")
    try:
        hub_db.update_server(server_id, name, body.label.strip(), vpn_endpoint)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail=f'A server named "{name}" already exists.')
    return {"ok": True}


@app.delete("/api/servers/{server_id}")
def delete_server(server_id: int, request: Request):
    require_auth(request)
    _require_server(server_id)
    hub_db.delete_server(server_id)
    return {"ok": True}


@app.websocket("/agent/ws")
async def agent_ws(websocket: WebSocket):
    await hub.handle_connection(websocket)


# ---------------- dashboard-state backup (admin.json only — see agent/backup.py for per-server) ----------------

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

        backup_mod.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        try:
            backup_mod.build_backup_tar(backup_mod.BACKUP_DIR / f"pre-restore-{stamp}.tar.gz")
        except Exception:
            pass  # best-effort; must not block a restore the admin explicitly requested

        try:
            backup_mod.restore_from_tar(Path(tmp.name))
        except (ValueError, tarfile.TarError) as e:
            raise HTTPException(status_code=400, detail=f"Restore failed: {e}")
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    # unlike the old single-box restore, this only touches admin.json — settings.load()
    # re-reads the file on every call, so there's no in-memory cache to invalidate and
    # no need to force a process restart.
    return {"ok": True}


# ---------------- per-server clients ----------------

@app.get("/api/servers/{server_id}/clients")
async def list_clients(server_id: int, request: Request):
    require_auth(request)
    _require_server(server_id)
    if hub.hub.is_online(server_id):
        result = await _call(server_id, "client.list")
        return result["clients"]
    return [dict(r) | {"stale": True} for r in hub_db.get_server_clients(server_id)]


@app.post("/api/servers/{server_id}/clients", status_code=201)
async def create_client(server_id: int, body: CreateClientBody, request: Request):
    require_auth(request)
    _require_server(server_id)
    name = body.name.strip()
    cfg = settings.load()
    validity_days = body.validity_days or cfg["client_default_validity_days"]
    data = {
        "name": name, "max_connections": body.max_connections, "quota_gb": body.quota_gb,
        "bandwidth_mbps": body.bandwidth_mbps, "validity_days": validity_days,
    }
    if body.team is not None:
        data["team"] = body.team
    if body.label is not None:
        data["label"] = body.label
    result = await _call(server_id, "client.create", data)
    return result


@app.get("/api/servers/{server_id}/clients/export")
async def export_clients(server_id: int, request: Request):
    require_auth(request)
    _require_server(server_id)
    result = await _call(server_id, "client.list")
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["name", "label", "team", "max_connections", "quota_gb", "bandwidth_mbps", "validity_days"])
    for c in result["clients"]:
        quota_gb = c["quota_bytes"] / 1_000_000_000 if c.get("quota_bytes") else ""
        bandwidth_mbps = c["bandwidth_kbps"] / 1000 if c.get("bandwidth_kbps") else ""
        validity_days = ""
        if c.get("created") and c.get("expires"):
            try:
                validity_days = (datetime.fromisoformat(c["expires"]) - datetime.fromisoformat(c["created"])).days
            except ValueError:
                pass
        writer.writerow([
            c["name"], c.get("label") or "", c.get("team") or "",
            c["max_connections"], quota_gb, bandwidth_mbps, validity_days,
        ])
    return Response(
        content=buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="clients-{server_id}.csv"'},
    )


@app.post("/api/servers/{server_id}/clients/import")
async def import_clients(server_id: int, request: Request, file: UploadFile):
    """Upsert by name: a row whose name doesn't exist yet issues a new cert
    (client.create); a row matching an existing client updates its limits/
    label/team in place (client.limits) rather than failing on conflict --
    that's what makes "export, edit in a spreadsheet, re-import" work as a
    bulk-edit flow instead of only being useful for bulk-creation once."""
    require_auth(request)
    _require_server(server_id)
    cfg = settings.load()
    text = (await file.read()).decode("utf-8-sig")
    created, updated, failed = [], [], []
    for row in _csv_rows(text):
        name = (row.get("name") or "").strip()
        if not name:
            continue
        limits = {
            "name": name,
            "max_connections": int(row["max_connections"]) if row.get("max_connections") else 1,
            "quota_gb": float(row["quota_gb"]) if row.get("quota_gb") else None,
            "bandwidth_mbps": float(row["bandwidth_mbps"]) if row.get("bandwidth_mbps") else None,
        }
        if row.get("team"):
            limits["team"] = row["team"]
        if row.get("label"):
            limits["label"] = row["label"]
        try:
            create_data = dict(limits, validity_days=int(row["validity_days"]) if row.get("validity_days") else cfg["client_default_validity_days"])
            await _call(server_id, "client.create", create_data)
            created.append(name)
        except HTTPException as e:
            if e.status_code != 409:
                failed.append({"name": name, "error": str(e.detail)})
                continue
            try:
                await _call(server_id, "client.limits", limits)
                updated.append(name)
            except HTTPException as e2:
                failed.append({"name": name, "error": str(e2.detail)})
    return {"created": created, "updated": updated, "failed": failed}


@app.get("/api/servers/{server_id}/clients/{name}/download")
async def download_client(server_id: int, name: str, request: Request, endpoint: str = "server"):
    require_auth(request)
    row = _require_server(server_id)
    if not row["vpn_endpoint"]:
        raise HTTPException(status_code=400, detail="This server has no VPN endpoint address configured yet.")
    result = await _call(server_id, "client.download", {
        "name": name, "endpoint": endpoint, "vpn_endpoint": row["vpn_endpoint"],
    })
    return Response(
        content=result["ovpn"],
        media_type="application/x-openvpn-profile",
        headers={"Content-Disposition": f'attachment; filename="{name}-{endpoint}.ovpn"'},
    )


@app.delete("/api/servers/{server_id}/clients/{name}")
async def revoke_client(server_id: int, name: str, request: Request):
    require_auth(request)
    _require_server(server_id)
    return await _call(server_id, "client.revoke", {"name": name})


@app.post("/api/servers/{server_id}/clients/{name}/renew")
async def renew_client(server_id: int, name: str, body: RenewBody, request: Request):
    require_auth(request)
    _require_server(server_id)
    return await _call(server_id, "client.renew", {"name": name, "validity_days": body.validity_days})


@app.put("/api/servers/{server_id}/clients/{name}/limits")
async def set_client_limits(server_id: int, name: str, body: LimitsBody, request: Request):
    require_auth(request)
    _require_server(server_id)
    data = {
        "name": name, "max_connections": body.max_connections,
        "quota_gb": body.quota_gb, "bandwidth_mbps": body.bandwidth_mbps,
    }
    if body.team is not None:
        data["team"] = body.team
    if body.label is not None:
        data["label"] = body.label
    return await _call(server_id, "client.limits", data)


@app.post("/api/servers/{server_id}/clients/{name}/reset-usage")
async def reset_client_usage(server_id: int, name: str, request: Request):
    require_auth(request)
    _require_server(server_id)
    return await _call(server_id, "client.reset_usage", {"name": name})


@app.put("/api/servers/{server_id}/clients/{name}/static-ip")
async def set_client_static_ip(server_id: int, name: str, body: SetStaticIpBody, request: Request):
    """Admin-only, explicit static IP pin — never exposed as a user choice.
    This is what a downstream product can key IP-based auth off safely: the
    assignment survives limits/bandwidth edits and only an admin can change it."""
    require_auth(request)
    _require_server(server_id)
    return await _call(server_id, "client.set_static_ip", {
        "name": name, "endpoint": body.endpoint, "static_ip": body.static_ip,
    })


# ---------------- per-server status + system ----------------

@app.get("/api/servers/{server_id}/status")
async def api_status(server_id: int, request: Request):
    require_auth(request)
    _require_server(server_id)
    if not hub.hub.is_online(server_id):
        return []
    result = await _call(server_id, "server.status")
    return result["sessions"]


@app.get("/api/servers/{server_id}/system")
async def api_system(server_id: int, request: Request):
    require_auth(request)
    row = _require_server(server_id)
    conn = hub.hub.get(server_id)
    cfg = settings.load()
    history = [dict(r) for r in hub_db.get_metrics(server_id, cfg["retention_points"])]
    return {
        "online": conn is not None,
        "latest": conn.last_telemetry if conn else (history[-1] if history else None),
        "history": history,
        "vpn_endpoint": row["vpn_endpoint"],
    }


# ---------------- per-server endpoints ----------------

@app.get("/api/servers/{server_id}/endpoints")
async def list_endpoints(server_id: int, request: Request):
    require_auth(request)
    _require_server(server_id)
    if hub.hub.is_online(server_id):
        result = await _call(server_id, "endpoint.list")
        return result["endpoints"]
    return [dict(r) | {"stale": True} for r in hub_db.get_server_endpoints(server_id)]


@app.post("/api/servers/{server_id}/endpoints", status_code=201)
async def create_endpoint(server_id: int, body: CreateEndpointBody, request: Request):
    require_auth(request)
    _require_server(server_id)
    return await _call(server_id, "endpoint.create", {
        "name": body.name, "port": body.port, "protocol": body.protocol,
        "cipher": body.cipher, "dns1": body.dns1, "dns2": body.dns2,
    })


@app.get("/api/servers/{server_id}/endpoints/export")
async def export_endpoints(server_id: int, request: Request):
    require_auth(request)
    _require_server(server_id)
    result = await _call(server_id, "endpoint.list")
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["name", "port", "protocol", "cipher", "dns1", "dns2"])
    for ep in result["endpoints"]:
        writer.writerow([ep["name"], ep["port"], ep["protocol"], ep["cipher"], ep.get("dns1") or "", ep.get("dns2") or ""])
    return Response(
        content=buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="endpoints-{server_id}.csv"'},
    )


@app.post("/api/servers/{server_id}/endpoints/import")
async def import_endpoints(server_id: int, request: Request, file: UploadFile):
    require_auth(request)
    _require_server(server_id)
    text = (await file.read()).decode("utf-8-sig")
    created, skipped, failed = [], [], []
    for row in _csv_rows(text):
        name = (row.get("name") or "").strip()
        if not name:
            continue
        data = {
            "name": name,
            "port": int(row["port"]),
            "protocol": (row.get("protocol") or "").strip().lower(),
            "cipher": (row.get("cipher") or "AES-128-GCM").strip(),
            "dns1": (row.get("dns1") or "1.1.1.1").strip(),
            "dns2": (row.get("dns2") or "1.0.0.1").strip(),
        }
        try:
            await _call(server_id, "endpoint.create", data)
            created.append(name)
        except HTTPException as e:
            # An endpoint's port/protocol are its identity -- unlike clients there's no
            # non-destructive "update" to fall back to, so a name that already exists is
            # left untouched rather than reported as a failure on re-import.
            (skipped if e.status_code == 409 else failed).append({"name": name, "error": str(e.detail)})
    return {"created": created, "skipped": skipped, "failed": failed}


@app.delete("/api/servers/{server_id}/endpoints/{name}")
async def delete_endpoint(server_id: int, name: str, request: Request):
    require_auth(request)
    _require_server(server_id)
    return await _call(server_id, "endpoint.delete", {"name": name})


@app.post("/api/servers/{server_id}/endpoints/{name}/restart")
async def restart_endpoint(server_id: int, name: str, request: Request):
    require_auth(request)
    _require_server(server_id)
    result = await _call(server_id, "endpoint.restart", {"name": name})
    asyncio.create_task(asyncio.to_thread(
        notify.notify, settings.load(), "endpoint_restart",
        f'Endpoint "{name}" on server {server_id} was restarted by the admin.',
    ))
    return result


@app.post("/api/servers/{server_id}/endpoints/{name}/stop")
async def stop_endpoint(server_id: int, name: str, request: Request):
    require_auth(request)
    _require_server(server_id)
    result = await _call(server_id, "endpoint.stop", {"name": name})
    asyncio.create_task(asyncio.to_thread(
        notify.notify, settings.load(), "endpoint_stop",
        f'Endpoint "{name}" on server {server_id} was stopped by the admin.',
    ))
    return result


@app.post("/api/servers/{server_id}/endpoints/{name}/start")
async def start_endpoint(server_id: int, name: str, request: Request):
    require_auth(request)
    _require_server(server_id)
    result = await _call(server_id, "endpoint.start", {"name": name})
    asyncio.create_task(asyncio.to_thread(
        notify.notify, settings.load(), "endpoint_start",
        f'Endpoint "{name}" on server {server_id} was started by the admin.',
    ))
    return result


@app.get("/api/servers/{server_id}/endpoints/{name}/logs")
async def endpoint_logs(server_id: int, name: str, request: Request):
    require_auth(request)
    _require_server(server_id)
    return await _call(server_id, "endpoint.logs", {"name": name})


@app.get("/api/servers/{server_id}/endpoints/{name}/config")
async def endpoint_config(server_id: int, name: str, request: Request):
    require_auth(request)
    _require_server(server_id)
    return await _call(server_id, "endpoint.config", {"name": name})


# ---------------- per-server backup (PKI, ccd, server confs, limits.db) ----------------

@app.get("/api/servers/{server_id}/backup")
async def download_server_backup(server_id: int, request: Request):
    require_auth(request)
    _require_server(server_id)
    result = await _call(server_id, "server.backup")
    content = base64.b64decode(result["content_b64"])
    tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
    tmp.write(content)
    tmp.close()
    dest = Path(tmp.name)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return FileResponse(
        dest, media_type="application/gzip",
        filename=f"ovpn-server-{server_id}-backup-{stamp}.tar.gz",
        background=BackgroundTask(dest.unlink, missing_ok=True),
    )


@app.post("/api/servers/{server_id}/backup/restore")
async def restore_server_backup(server_id: int, request: Request, file: UploadFile):
    require_auth(request)
    _require_server(server_id)
    if not file.filename.endswith((".tar.gz", ".tgz")):
        raise HTTPException(status_code=400, detail="Expected a .tar.gz backup file.")
    content = await file.read()
    result = await _call(server_id, "server.restore", {"content_b64": base64.b64encode(content).decode("ascii")})
    return {"ok": True, "endpoints_restored": result["endpoints_restored"]}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    # StaticFiles only sets ETag/Last-Modified, no Cache-Control — browsers are free to
    # apply heuristic caching and skip revalidation entirely, so a deploy can silently
    # keep serving stale CSS/JS with no way to tell short of a hard refresh. "no-cache"
    # forces revalidation on every load (still gets a cheap 304 when nothing changed);
    # it does not mean "don't cache."
    response = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.exception_handler(404)
async def spa_fallback(request: Request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=404, content={"detail": "Not found"})
    return FileResponse(STATIC_DIR / "index.html")
