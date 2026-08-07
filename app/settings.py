import json
import os
from pathlib import Path

ADMIN_FILE = Path("/etc/ovpn-dashboard/admin.json")

DEFAULTS = {
    "session_epoch": 0,
    "session_timeout_minutes": 0,  # 0 = no idle timeout
    "lockout_max_attempts": 5,
    "lockout_seconds": 300,
    "notify_webhook_enabled": False,
    "notify_webhook_url": "",
    "notify_telegram_enabled": False,
    "notify_telegram_bot_token": "",
    "notify_telegram_chat_id": "",
    "client_default_max_connections": 1,
    "client_default_quota_gb": None,
    "client_default_bandwidth_mbps": None,
    "client_default_validity_days": 365,
    "retention_points": 60,
}


def _read_raw() -> dict:
    if ADMIN_FILE.exists():
        return json.loads(ADMIN_FILE.read_text())
    data = {
        "hash": os.environ["ADMIN_PASSWORD_HASH"],
        "salt": os.environ["ADMIN_PASSWORD_SALT"],
    }
    ADMIN_FILE.parent.mkdir(parents=True, exist_ok=True)
    ADMIN_FILE.write_text(json.dumps(data))
    ADMIN_FILE.chmod(0o600)
    return data


def _write_raw(data: dict) -> None:
    ADMIN_FILE.write_text(json.dumps(data))
    ADMIN_FILE.chmod(0o600)


def load() -> dict:
    """Merged view: stored file (hash/salt + any settings ever saved) over defaults."""
    return {**DEFAULTS, **_read_raw()}


def update(patch: dict) -> dict:
    data = _read_raw()
    data.update(patch)
    _write_raw(data)
    return {**DEFAULTS, **data}
