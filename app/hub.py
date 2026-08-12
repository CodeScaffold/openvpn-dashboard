"""WebSocket server side of the hub<->agent link: connection registry, RPC
request/response correlation, and the enrollment/handshake auth. The actual
route (`@app.websocket("/agent/ws")`) lives in main.py, matching this
codebase's existing single-file routing convention — this module holds the
logic, main.py wires the endpoint.
"""
import asyncio
import hashlib
import logging
import secrets
import time

from fastapi import WebSocket, WebSocketDisconnect

from common.wire import PROTOCOL_VERSION, Envelope, HelloData, request
from . import hub_db, notify, settings

log = logging.getLogger("ovpn-dashboard.hub")

ENROLL_TOKEN_BYTES = 32
CALL_TIMEOUT_SECONDS = 30

# ---- brute-force backoff for the agent handshake (same shape as main.py's admin-login lockout,
# kept independent here to avoid a hub<->main import cycle) ----
_failed_attempts: dict[str, list[float]] = {}
LOCKOUT_MAX_ATTEMPTS = 10
LOCKOUT_SECONDS = 60


def _is_locked_out(key: str) -> bool:
    attempts = _failed_attempts.get(key, [])
    recent = [t for t in attempts if time.time() - t < LOCKOUT_SECONDS]
    _failed_attempts[key] = recent
    return len(recent) >= LOCKOUT_MAX_ATTEMPTS


def _record_failure(key: str) -> None:
    _failed_attempts.setdefault(key, []).append(time.time())


def _clear_failures(key: str) -> None:
    _failed_attempts.pop(key, None)


def generate_enrollment(name: str, vpn_endpoint: str, label: str = "") -> tuple[int, str]:
    """Creates a server row and returns (server_id, plaintext_token). The
    plaintext is shown to the admin exactly once by the caller — only its
    hash is ever persisted."""
    token = secrets.token_urlsafe(ENROLL_TOKEN_BYTES)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    server_id = hub_db.create_server(name, label, token_hash, vpn_endpoint)
    return server_id, token


class RemoteOpError(RuntimeError):
    """An op failed on the agent. code mirrors agent/ops.py's OpError.code
    ("not_found" | "conflict" | "bad_request" | "internal") so route
    handlers can map it to an HTTP status without string-matching."""

    def __init__(self, message: str, code: str | None):
        super().__init__(message)
        self.code = code or "bad_request"


class AgentConnection:
    """One live WebSocket link to a single agent. Lets hub-side callers
    `await conn.call(op, data)` like a normal RPC by correlating responses
    to requests via envelope id, while telemetry/notify events arrive on the
    same socket independently of any pending call."""

    def __init__(self, server_id: int, websocket: WebSocket):
        self.server_id = server_id
        self.websocket = websocket
        self._pending: dict[str, asyncio.Future] = {}
        self.last_telemetry: dict | None = None

    async def call(self, op: str, data: dict | None = None, timeout: float = CALL_TIMEOUT_SECONDS) -> dict:
        req = request(op, data)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req.id] = fut
        try:
            await self.websocket.send_text(req.to_json())
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(req.id, None)

    def resolve(self, envelope: Envelope) -> None:
        fut = self._pending.get(envelope.id)
        if fut is None or fut.done():
            return
        if envelope.error:
            fut.set_exception(RemoteOpError(envelope.error, envelope.error_code))
        else:
            fut.set_result(envelope.data)

    def fail_all_pending(self, reason: str) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError(reason))
        self._pending.clear()


class Hub:
    """Registry of live agent connections, keyed by server_id."""

    def __init__(self):
        self._connections: dict[int, AgentConnection] = {}

    def get(self, server_id: int) -> AgentConnection | None:
        return self._connections.get(server_id)

    def is_online(self, server_id: int) -> bool:
        return server_id in self._connections

    def online_ids(self) -> set[int]:
        return set(self._connections.keys())

    async def call(self, server_id: int, op: str, data: dict | None = None) -> dict:
        conn = self._connections.get(server_id)
        if conn is None:
            raise ConnectionError("Server is not currently connected.")
        return await conn.call(op, data)

    def register(self, server_id: int, conn: AgentConnection) -> None:
        existing = self._connections.get(server_id)
        if existing is not None:
            # a second agent dialing in with the same token replaces the stale link
            existing.fail_all_pending("Superseded by a new connection.")
        self._connections[server_id] = conn

    def unregister(self, server_id: int, conn: AgentConnection) -> None:
        if self._connections.get(server_id) is conn:
            del self._connections[server_id]
        conn.fail_all_pending("Agent disconnected.")


hub = Hub()


async def handle_connection(websocket: WebSocket) -> None:
    """The full lifecycle of one agent's WebSocket: handshake, register,
    receive loop, cleanup on disconnect."""
    peer = websocket.client.host if websocket.client else "unknown"
    await websocket.accept()

    if _is_locked_out(peer):
        await websocket.close(code=4001, reason="Too many failed attempts, try later.")
        return

    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        return

    try:
        hello_env = Envelope.from_json(raw)
        if hello_env.kind != "event" or hello_env.op != "hello":
            raise ValueError("First message must be a hello event.")
        hello = HelloData(**hello_env.data)
    except (ValueError, TypeError):
        _record_failure(peer)
        await websocket.close(code=4002, reason="Malformed hello.")
        return

    token_hash = hashlib.sha256(hello.token.encode()).hexdigest()
    row = hub_db.get_server_by_token_hash(token_hash)
    if row is None:
        _record_failure(peer)
        log.warning("agent handshake rejected: unknown token from %s", peer)
        await websocket.close(code=4003, reason="Unknown or revoked token.")
        return

    if hello.protocol_version != PROTOCOL_VERSION:
        _record_failure(peer)
        await websocket.close(code=4004, reason=f"Protocol version mismatch (hub={PROTOCOL_VERSION}).")
        return

    _clear_failures(peer)
    server_id = row["id"]
    hub_db.touch_server(server_id, hello.agent_version, hello.hostname, hello.public_addr)
    log.info('agent "%s" (server_id=%s) connected from %s', row["name"], server_id, peer)

    conn = AgentConnection(server_id, websocket)
    hub.register(server_id, conn)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                envelope = Envelope.from_json(raw)
            except Exception:
                log.warning("malformed envelope from server_id=%s, dropping", server_id)
                continue

            if envelope.kind == "res":
                conn.resolve(envelope)
            elif envelope.kind == "event":
                await _handle_event(server_id, conn, envelope)
    except WebSocketDisconnect:
        pass
    finally:
        hub.unregister(server_id, conn)
        log.info('agent "%s" (server_id=%s) disconnected', row["name"], server_id)


def _persist_telemetry(server_id: int, data: dict) -> None:
    """Feeds the hub.db display mirror. Runs off the event loop (sqlite
    writes, even fast local ones, shouldn't compete with WS I/O for every
    other connected agent)."""
    hub_db.touch_last_seen(server_id)
    if "endpoints" in data:
        hub_db.replace_server_endpoints(server_id, data["endpoints"])
    if "clients" in data:
        hub_db.replace_server_clients(server_id, data["clients"])
    hub_db.insert_metric_point(server_id, data)
    hub_db.prune_metrics(server_id, settings.load()["retention_points"])


async def _handle_event(server_id: int, conn: AgentConnection, envelope: Envelope) -> None:
    if envelope.op == "telemetry":
        conn.last_telemetry = envelope.data
        asyncio.create_task(asyncio.to_thread(_persist_telemetry, server_id, envelope.data))
    elif envelope.op == "notify":
        cfg = settings.load()
        # notify.notify() does blocking network I/O (webhook/Telegram); keep it off the
        # event loop so a slow webhook can't stall every other agent's WS traffic.
        asyncio.create_task(
            asyncio.to_thread(notify.notify, cfg, envelope.data.get("event", "agent_event"), envelope.data.get("message", ""))
        )
    elif envelope.op == "bye":
        pass
