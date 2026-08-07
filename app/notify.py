import json
import logging
import urllib.request

log = logging.getLogger("ovpn-dashboard.notify")

TIMEOUT = 8


def send_webhook(url: str, event: str, message: str) -> None:
    body = json.dumps({"event": event, "message": message}).encode()
    req = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        resp.read()


def send_telegram(bot_token: str, chat_id: str, message: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": message}).encode()
    req = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        resp.read()


def notify(settings: dict, event: str, message: str) -> None:
    """Best-effort fan-out. Never raises — a broken webhook must not break enforcement."""
    if settings.get("notify_webhook_enabled") and settings.get("notify_webhook_url"):
        try:
            send_webhook(settings["notify_webhook_url"], event, message)
        except Exception:
            log.exception("webhook notification failed (event=%s)", event)
    if settings.get("notify_telegram_enabled") and settings.get("notify_telegram_bot_token") and settings.get("notify_telegram_chat_id"):
        try:
            send_telegram(settings["notify_telegram_bot_token"], settings["notify_telegram_chat_id"], f"[OpenVPN Admin] {message}")
        except Exception:
            log.exception("telegram notification failed (event=%s)", event)
