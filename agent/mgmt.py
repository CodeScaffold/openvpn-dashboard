import socket

from common.wire import Session

MGMT_HOST = "127.0.0.1"


def _read_until_end(sock: socket.socket, timeout: float = 4.0) -> str:
    sock.settimeout(timeout)
    chunks = []
    buf = b""
    while True:
        data = sock.recv(65536)
        if not data:
            break
        buf += data
        if b"\nEND" in buf or buf.strip().endswith(b"END"):
            chunks.append(buf)
            break
    return b"".join(chunks or [buf]).decode(errors="replace")


def _connect(mgmt_port: int) -> socket.socket:
    s = socket.create_connection((MGMT_HOST, mgmt_port), timeout=4)
    s.settimeout(4)
    s.recv(4096)  # consume the >INFO banner line
    return s


def get_sessions(mgmt_port: int, endpoint: str = "") -> list[Session]:
    s = _connect(mgmt_port)
    try:
        s.sendall(b"status 2\n")
        raw = _read_until_end(s)
    finally:
        s.close()

    header_fields: list[str] = []
    sessions: list[Session] = []
    for line in raw.splitlines():
        if line.startswith("HEADER,CLIENT_LIST,"):
            header_fields = line.split(",")[2:]
        elif line.startswith("CLIENT_LIST,"):
            values = line.split(",")[1:]
            row = dict(zip(header_fields, values))
            try:
                sessions.append(
                    Session(
                        name=row.get("Common Name", ""),
                        real_address=row.get("Real Address", ""),
                        virtual_address=row.get("Virtual Address", ""),
                        bytes_received=int(row.get("Bytes Received", 0) or 0),
                        bytes_sent=int(row.get("Bytes Sent", 0) or 0),
                        connected_since=row.get("Connected Since", ""),
                        connected_since_t=int(row.get("Connected Since (time_t)", 0) or 0),
                        client_id=row.get("Client ID", ""),
                        endpoint=endpoint,
                    )
                )
            except (ValueError, TypeError):
                continue
    return sessions


def get_all_sessions(endpoints) -> list[Session]:
    """endpoints: iterable of db endpoint rows (need .name, .mgmt_port)."""
    sessions: list[Session] = []
    for ep in endpoints:
        try:
            sessions.extend(get_sessions(ep["mgmt_port"], endpoint=ep["name"]))
        except OSError:
            continue
    return sessions


def kill_client_id(mgmt_port: int, client_id: str) -> None:
    s = _connect(mgmt_port)
    try:
        s.sendall(f"client-kill {client_id}\n".encode())
        s.settimeout(2)
        try:
            s.recv(4096)
        except socket.timeout:
            pass
    finally:
        s.close()
