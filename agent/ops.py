"""The agent's op dispatch table. Every op is a plain function taking and
returning a dict, so it can be called identically from three places:
the WebSocket dispatcher, `agent/main.py --selftest`, and unit tests. No
FastAPI/Request/HTTPException here on purpose — those are hub/HTTP concerns,
not agent ones.

Business logic here is a straight move from the old app/main.py route
handlers, decoupled from FastAPI.
"""
import base64
import ipaddress
import re
import subprocess
import tempfile
from pathlib import Path

from . import backup as backup_mod, db, endpoints as endpoints_mod, mgmt, netlimit, pki

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
ALLOWED_CIPHERS = {"AES-128-GCM", "AES-256-GCM", "CHACHA20-POLY1305"}
SERVER_DIR = Path("/etc/openvpn/server")


class OpError(Exception):
    """Raised for an expected, user-facing failure. code is a coarse
    HTTP-shaped hint ("not_found" | "conflict" | "bad_request" | "internal")
    that crosses the wire alongside the message so the hub can map it to the
    right status code without string-matching."""

    def __init__(self, message: str, code: str = "bad_request"):
        super().__init__(message)
        self.code = code


# ---------------- views ----------------

def client_view(name: str, expires: str | None = None) -> dict:
    db.ensure_client_row(name)
    row = db.get_limits(name)
    if expires is None:
        expires = pki.read_client_index().get(name, {}).get("expires")
    ips = db.get_client_ips(name)
    pins = db.get_client_ip_pins(name)
    return {
        "name": name,
        "created": pki.client_created_at(name),
        "expires": expires,
        "max_connections": row["max_connections"],
        "quota_bytes": row["quota_bytes"],
        "used_bytes": row["used_bytes"],
        "bandwidth_kbps": row["bandwidth_kbps"],
        "disabled": bool(row["disabled"]),
        "team": row["team"],
        "static_ips": {ep: {"static_ip": ip, "pinned": pins.get(ep, False)} for ep, ip in ips.items()},
    }


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


# ---------------- limits (shared by client.create / client.limits) ----------------

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
                    # a pre-existing (possibly pinned) IP is reused as-is; only a client
                    # with no IP at all on this endpoint gets a fresh, unpinned one
                    used = db.get_endpoint_used_ips(ep["name"])
                    ip = db.allocate_static_ip(ep["subnet"], used)
                    db.set_client_ip(name, ep["name"], ip)
                netlimit.apply_bandwidth(ep["tun_dev"], ip, bandwidth_kbps)
        else:
            for ep in all_endpoints:
                ip = client_ips.get(ep["name"])
                if ip:
                    # tc shaping always comes off when there's no cap, regardless of pin —
                    # the pin only governs whether the IP *assignment* itself survives below
                    netlimit.remove_bandwidth(ep["tun_dev"], ip)
            # only the auto-allocated (unpinned) IPs are ephemeral to the bandwidth feature;
            # an admin-pinned static IP (client.set_static_ip) must survive this unrelated edit
            db.clear_unpinned_client_ips(name)
    except RuntimeError as e:
        raise OpError(f"Failed to apply bandwidth limit: {e}", code="internal")

    db.update_limits(name, max_connections=max_connections, quota_bytes=quota_bytes, bandwidth_kbps=bandwidth_kbps)

    client_ips = db.get_client_ips(name)
    for ep in all_endpoints:
        netlimit.write_ccd(ep["ccd_dir"], name, client_ips.get(ep["name"]), disabled=bool(row["disabled"]))


# ---------------- client ops ----------------

def op_client_list(data: dict) -> dict:
    index = pki.read_client_index()
    valid = sorted(n for n, info in index.items() if info["status"] == "V")
    return {"clients": [client_view(n, index[n]["expires"]) for n in valid]}


def op_client_create(data: dict) -> dict:
    name = data["name"].strip()
    max_connections = data.get("max_connections", 1)
    quota_gb = data.get("quota_gb")
    bandwidth_mbps = data.get("bandwidth_mbps")
    validity_days = data["validity_days"]  # hub resolves the client-default fallback; agent has no settings

    if not NAME_RE.match(name):
        raise OpError("Use only letters, numbers, - and _ (max 32 chars).")
    if name.lower() in pki.RESERVED_NAMES:
        raise OpError("That name is reserved.")
    if name in pki.read_valid_client_names():
        raise OpError("A client with that name already exists.", code="conflict")
    if (pki.EASYRSA_DIR / "pki" / "reqs" / f"{name}.req").exists():
        raise OpError("That name was used before (even if revoked). Pick another.", code="conflict")
    if not (1 <= max_connections <= 10):
        raise OpError("max_connections must be between 1 and 10.")
    if not (30 <= validity_days <= 3650):
        raise OpError("Certificate validity must be between 30 and 3650 days.")

    try:
        pki.run_easyrsa("gen-req", name, "nopass")
        pki.run_easyrsa("sign-req", "client", name, extra_env={"EASYRSA_CERT_EXPIRE": str(validity_days)})
    except RuntimeError as e:
        raise OpError(str(e), code="internal")

    db.ensure_client_row(name)
    if max_connections != 1 or quota_gb or bandwidth_mbps:
        apply_limits(name, max_connections, quota_gb, bandwidth_mbps)
    if "team" in data:
        db.update_limits(name, team=data["team"])

    return client_view(name)


def op_client_download(data: dict) -> dict:
    name = data["name"]
    endpoint = data.get("endpoint", "server")
    vpn_endpoint = data["vpn_endpoint"]  # hub supplies this server's public address
    if name not in pki.read_valid_client_names():
        raise OpError("Client not found.", code="not_found")
    try:
        ovpn = pki.build_ovpn(name, endpoint, vpn_endpoint)
    except ValueError as e:
        raise OpError(str(e), code="not_found")
    return {"ovpn": ovpn}


def op_client_revoke(data: dict) -> dict:
    name = data["name"]
    if name.lower() in pki.RESERVED_NAMES:
        raise OpError("Refusing to revoke the server cert.")
    if name not in pki.read_valid_client_names():
        raise OpError("Client not found.", code="not_found")

    try:
        pki.run_easyrsa("revoke", name)
        pki.refresh_crl()
    except RuntimeError as e:
        raise OpError(str(e), code="internal")

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


def op_client_renew(data: dict) -> dict:
    name = data["name"]
    validity_days = data["validity_days"]
    if name not in pki.read_valid_client_names():
        raise OpError("Client not found.", code="not_found")
    if not (30 <= validity_days <= 3650):
        raise OpError("Certificate validity must be between 30 and 3650 days.")

    # Same-name reissue: revoke moves the old req/cert/key out of pki/reqs, pki/issued,
    # pki/private (into revoked/*_by_serial), so gen-req can freely recreate them under
    # the same CN. Limits, quota usage, ccd and bandwidth rules are untouched — this is
    # a cert swap, not a new client.
    try:
        pki.run_easyrsa("revoke", name)
        pki.refresh_crl()
        pki.run_easyrsa("gen-req", name, "nopass")
        pki.run_easyrsa("sign-req", "client", name, extra_env={"EASYRSA_CERT_EXPIRE": str(validity_days)})
    except RuntimeError as e:
        raise OpError(str(e), code="internal")

    return client_view(name)


def op_client_limits(data: dict) -> dict:
    name = data["name"]
    max_connections = data.get("max_connections", 1)
    quota_gb = data.get("quota_gb")
    bandwidth_mbps = data.get("bandwidth_mbps")
    if name not in pki.read_valid_client_names():
        raise OpError("Client not found.", code="not_found")
    if not (1 <= max_connections <= 10):
        raise OpError("max_connections must be between 1 and 10.")
    if quota_gb is not None and quota_gb <= 0:
        raise OpError("quota_gb must be positive, or omitted for unlimited.")
    if bandwidth_mbps is not None and bandwidth_mbps <= 0:
        raise OpError("bandwidth_mbps must be positive, or omitted for unlimited.")

    apply_limits(name, max_connections, quota_gb, bandwidth_mbps)
    if "team" in data:
        db.update_limits(name, team=data["team"])
    return client_view(name)


def op_client_reset_usage(data: dict) -> dict:
    name = data["name"]
    if name not in pki.read_valid_client_names():
        raise OpError("Client not found.", code="not_found")
    db.reset_usage(name)
    client_ips = db.get_client_ips(name)
    for ep in db.get_all_endpoints():
        netlimit.write_ccd(ep["ccd_dir"], name, client_ips.get(ep["name"]), disabled=False)
    return client_view(name)


def op_client_set_static_ip(data: dict) -> dict:
    """Admin-only, explicit IP assignment — pinned, so it survives unrelated
    limits edits (see apply_limits). This is what makes the client's exit IP
    stable enough for a downstream product to authenticate by source IP:
    the user themselves never chooses it, and it doesn't drift."""
    name = data["name"]
    endpoint = data["endpoint"]
    static_ip = (data.get("static_ip") or "").strip() or None

    if name not in pki.read_valid_client_names():
        raise OpError("Client not found.", code="not_found")
    ep = db.get_endpoint(endpoint)
    if not ep:
        raise OpError("Endpoint not found.", code="not_found")

    if static_ip is None:
        db.clear_client_ip(name, endpoint)
    else:
        try:
            addr = ipaddress.ip_address(static_ip)
        except ValueError:
            raise OpError(f'"{static_ip}" is not a valid IP address.')
        network = ipaddress.ip_network(ep["subnet"], strict=False)
        if addr not in network:
            raise OpError(f"{static_ip} is not inside this endpoint's subnet ({ep['subnet']}).")
        owner = db.get_ip_owner(endpoint, static_ip)
        if owner and owner != name:
            raise OpError(f'{static_ip} is already assigned to client "{owner}" on this endpoint.', code="conflict")
        db.set_client_ip(name, endpoint, static_ip, pinned=True)

    row = db.get_limits(name)
    client_ips = db.get_client_ips(name)
    netlimit.write_ccd(ep["ccd_dir"], name, client_ips.get(endpoint), disabled=bool(row["disabled"]))
    return client_view(name)


# ---------------- endpoint ops ----------------

def op_endpoint_list(data: dict) -> dict:
    return {"endpoints": [endpoint_view(ep) for ep in db.get_all_endpoints()]}


def op_endpoint_create(data: dict) -> dict:
    name, port, protocol, cipher = data["name"], data["port"], data["protocol"], data["cipher"]
    dns1, dns2 = data.get("dns1", ""), data.get("dns2", "")
    if cipher not in ALLOWED_CIPHERS:
        raise OpError(f"cipher must be one of {sorted(ALLOWED_CIPHERS)}.")
    try:
        endpoints_mod.create_endpoint(name, port, protocol, cipher, dns1, dns2)
    except ValueError as e:
        raise OpError(str(e), code="conflict" if "already exists" in str(e) else "bad_request")
    except RuntimeError as e:
        raise OpError(str(e), code="internal")

    # any client with a bandwidth cap needs a static IP + tc rule on the new endpoint too
    for cname, row in db.get_all_limits().items():
        if row["bandwidth_kbps"]:
            apply_limits(
                cname, row["max_connections"],
                row["quota_bytes"] / 1_000_000_000 if row["quota_bytes"] else None,
                row["bandwidth_kbps"] / 1000,
            )

    return endpoint_view(db.get_endpoint(name))


def op_endpoint_delete(data: dict) -> dict:
    try:
        endpoints_mod.delete_endpoint(data["name"])
    except ValueError as e:
        raise OpError(str(e), code="not_found" if "No endpoint" in str(e) else "bad_request")
    return {"ok": True}


def _require_endpoint(name: str):
    ep = db.get_endpoint(name)
    if not ep:
        raise OpError("Endpoint not found.", code="not_found")
    return ep


def op_endpoint_start(data: dict) -> dict:
    name = data["name"]
    _require_endpoint(name)
    result = subprocess.run(
        ["sudo", "-n", endpoints_mod.ENDPOINT_CTL, "enable-now", name], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise OpError(f"Start failed: {result.stderr.strip()[-300:]}", code="internal")
    return {"ok": True}


def op_endpoint_stop(data: dict) -> dict:
    name = data["name"]
    _require_endpoint(name)
    result = subprocess.run(
        ["sudo", "-n", endpoints_mod.ENDPOINT_CTL, "disable-now", name], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise OpError(f"Stop failed: {result.stderr.strip()[-300:]}", code="internal")
    return {"ok": True}


def op_endpoint_restart(data: dict) -> dict:
    # Every endpoint is a normal openvpn-server@<name> unit (README:"Architecture, in
    # one paragraph") — the old dashboard route only allowed name == "server" here,
    # which was a leftover from before multi-endpoint existed; start/stop were already
    # generic. Restart now matches them.
    name = data["name"]
    _require_endpoint(name)
    result = subprocess.run(
        ["sudo", "-n", endpoints_mod.ENDPOINT_CTL, "restart", name], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise OpError(f"Restart failed: {result.stderr.strip()[-300:]}", code="internal")
    return {"ok": True}


def op_endpoint_logs(data: dict) -> dict:
    name = data["name"]
    _require_endpoint(name)
    result = subprocess.run(
        ["journalctl", "-u", f"openvpn-server@{name}", "-n", "200", "--no-pager", "-o", "short-iso"],
        capture_output=True, text=True,
    )
    return {"logs": result.stdout or result.stderr}


def op_endpoint_config(data: dict) -> dict:
    name = data["name"]
    _require_endpoint(name)
    conf_path = SERVER_DIR / f"{name}.conf"
    if not conf_path.exists():
        raise OpError("Config file not found.", code="not_found")
    return {"config": conf_path.read_text()}


# ---------------- server ops ----------------

def op_server_status(data: dict) -> dict:
    sessions = mgmt.get_all_sessions(db.get_all_endpoints())
    return {
        "sessions": [
            {
                "name": s.name, "real_address": s.real_address, "virtual_address": s.virtual_address,
                "bytes_received": s.bytes_received, "bytes_sent": s.bytes_sent,
                "connected_since": s.connected_since, "client_id": s.client_id, "endpoint": s.endpoint,
            }
            for s in sessions
        ]
    }


def op_server_backup(data: dict) -> dict:
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as f:
        dest = Path(f.name)
    try:
        backup_mod.build_backup_tar(dest)
        content = dest.read_bytes()
    finally:
        dest.unlink(missing_ok=True)
    # base64 over the WS text frame — this box's PKI/ccd/limits.db is small (KBs–low MBs),
    # not worth a separate binary transfer channel for v1.
    return {"content_b64": base64.b64encode(content).decode("ascii")}


def op_server_restore(data: dict) -> dict:
    content = base64.b64decode(data["content_b64"])
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as f:
        f.write(content)
        src = Path(f.name)
    try:
        touched = backup_mod.restore_from_tar(src)
    except ValueError as e:
        raise OpError(str(e), code="bad_request")
    finally:
        src.unlink(missing_ok=True)
    return {"endpoints_restored": touched}


OPS = {
    "client.list": op_client_list,
    "client.create": op_client_create,
    "client.download": op_client_download,
    "client.revoke": op_client_revoke,
    "client.renew": op_client_renew,
    "client.limits": op_client_limits,
    "client.reset_usage": op_client_reset_usage,
    "client.set_static_ip": op_client_set_static_ip,
    "endpoint.list": op_endpoint_list,
    "endpoint.create": op_endpoint_create,
    "endpoint.delete": op_endpoint_delete,
    "endpoint.start": op_endpoint_start,
    "endpoint.stop": op_endpoint_stop,
    "endpoint.restart": op_endpoint_restart,
    "endpoint.logs": op_endpoint_logs,
    "endpoint.config": op_endpoint_config,
    "server.status": op_server_status,
    "server.backup": op_server_backup,
    "server.restore": op_server_restore,
}


def dispatch(op: str, data: dict) -> dict:
    handler = OPS.get(op)
    if handler is None:
        raise OpError(f'Unknown op "{op}".', code="not_found")
    return handler(data)
