"""Wire types shared between the hub (dashboard) and the agent running on
each managed OpenVPN box. Both sides import this module so the WebSocket
message shape and the session-telemetry shape never drift out of sync."""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

PROTOCOL_VERSION = 1

Kind = Literal["req", "res", "event"]


@dataclass
class Envelope:
    kind: Kind
    op: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    # coarse HTTP-shaped hint ("not_found" | "conflict" | "bad_request" | "internal"),
    # set only alongside error — lets the hub map an op failure to the right status code
    # without string-matching the message.
    error_code: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(raw: str) -> "Envelope":
        obj = json.loads(raw)
        return Envelope(
            kind=obj["kind"],
            op=obj["op"],
            id=obj.get("id") or uuid.uuid4().hex,
            data=obj.get("data") or {},
            error=obj.get("error"),
            error_code=obj.get("error_code"),
        )


def request(op: str, data: dict[str, Any] | None = None) -> Envelope:
    return Envelope(kind="req", op=op, data=data or {})


def response(
    req: Envelope, data: dict[str, Any] | None = None, error: str | None = None, error_code: str | None = None,
) -> Envelope:
    return Envelope(kind="res", op=req.op, id=req.id, data=data or {}, error=error, error_code=error_code)


def event(op: str, data: dict[str, Any] | None = None) -> Envelope:
    return Envelope(kind="event", op=op, data=data or {})


@dataclass
class Session:
    """A live OpenVPN client session, parsed from the management socket by
    the agent and forwarded to the hub as telemetry. Was app/mgmt.py:Session."""
    name: str
    real_address: str
    virtual_address: str
    bytes_received: int
    bytes_sent: int
    connected_since: str
    connected_since_t: int
    client_id: str
    endpoint: str = ""


@dataclass
class HelloData:
    """Sent by the agent immediately after the WebSocket connects. The hub
    resolves server_id from the token only — hostname/public_addr here are
    display metadata, never used for identity."""
    token: str
    agent_version: str
    hostname: str
    public_addr: str = ""
    protocol_version: int = PROTOCOL_VERSION
