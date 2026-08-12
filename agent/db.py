import ipaddress
import sqlite3
import threading
from pathlib import Path

from common.migrate import apply_migrations

DB_PATH = Path("/etc/openvpn/clients/limits.db")

_local = threading.local()


def _migration_1(conn: sqlite3.Connection) -> None:
    """Base schema. Also folds in the old pre-multi-endpoint
    _migrate_session_baseline hack: that table used to be (client_id, name,
    bytes_seen) only, and CREATE TABLE IF NOT EXISTS is a no-op against an
    existing table with the wrong shape, so detect and rebuild it first —
    the data is just a rolling accumulation baseline, safe to drop."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(session_baseline)").fetchall()}
    if cols and "endpoint_name" not in cols:
        conn.execute("DROP TABLE session_baseline")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS client_limits (
            name TEXT PRIMARY KEY,
            max_connections INTEGER NOT NULL DEFAULT 1,
            quota_bytes INTEGER,
            used_bytes INTEGER NOT NULL DEFAULT 0,
            bandwidth_kbps INTEGER,
            static_ip TEXT,
            disabled INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS endpoints (
            name TEXT PRIMARY KEY,
            port INTEGER NOT NULL,
            protocol TEXT NOT NULL,
            cipher TEXT NOT NULL,
            mgmt_port INTEGER NOT NULL,
            tun_dev TEXT NOT NULL,
            subnet TEXT NOT NULL,
            dns1 TEXT,
            dns2 TEXT,
            ccd_dir TEXT NOT NULL,
            created TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS client_endpoint_ip (
            client_name TEXT NOT NULL,
            endpoint_name TEXT NOT NULL,
            static_ip TEXT NOT NULL,
            PRIMARY KEY (client_name, endpoint_name)
        );

        CREATE TABLE IF NOT EXISTS session_baseline (
            endpoint_name TEXT NOT NULL,
            client_id TEXT NOT NULL,
            name TEXT NOT NULL,
            bytes_seen INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (endpoint_name, client_id)
        );
    """)


def _migration_2(conn: sqlite3.Connection) -> None:
    """Static IPs become admin-pinnable and independent of bandwidth caps
    (see apply_limits: removing a bandwidth cap used to silently strip the
    client's static IP). team groups clients for the multi-server UI."""
    conn.execute("ALTER TABLE client_endpoint_ip ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE client_limits ADD COLUMN team TEXT")


MIGRATIONS = [
    (1, "base schema", _migration_1),
    (2, "pinned static ips + team", _migration_2),
]


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        apply_migrations(conn, MIGRATIONS)
        _local.conn = conn
    return conn


# ---------------- clients ----------------

def ensure_client_row(name: str) -> None:
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO client_limits (name) VALUES (?)", (name,))
    conn.commit()


def get_limits(name: str) -> sqlite3.Row | None:
    conn = get_conn()
    return conn.execute("SELECT * FROM client_limits WHERE name = ?", (name,)).fetchone()


def get_all_limits() -> dict[str, sqlite3.Row]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM client_limits").fetchall()
    return {r["name"]: r for r in rows}


def update_limits(
    name: str,
    max_connections: int | None = None,
    quota_bytes: int | None = "unset",
    bandwidth_kbps: int | None = "unset",
    disabled: int | None = None,
    team: str | None = "unset",
) -> None:
    ensure_client_row(name)
    conn = get_conn()
    fields = []
    values = []
    if max_connections is not None:
        fields.append("max_connections = ?")
        values.append(max_connections)
    if quota_bytes != "unset":
        fields.append("quota_bytes = ?")
        values.append(quota_bytes)
    if bandwidth_kbps != "unset":
        fields.append("bandwidth_kbps = ?")
        values.append(bandwidth_kbps)
    if disabled is not None:
        fields.append("disabled = ?")
        values.append(disabled)
    if team != "unset":
        fields.append("team = ?")
        values.append(team)
    if not fields:
        return
    values.append(name)
    conn.execute(f"UPDATE client_limits SET {', '.join(fields)} WHERE name = ?", values)
    conn.commit()


def add_used_bytes(name: str, delta: int) -> int:
    conn = get_conn()
    conn.execute("UPDATE client_limits SET used_bytes = used_bytes + ? WHERE name = ?", (delta, name))
    conn.commit()
    row = conn.execute("SELECT used_bytes FROM client_limits WHERE name = ?", (name,)).fetchone()
    return row["used_bytes"] if row else 0


def reset_usage(name: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE client_limits SET used_bytes = 0, disabled = 0 WHERE name = ?", (name,))
    conn.commit()


def delete_client(name: str) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM client_limits WHERE name = ?", (name,))
    conn.execute("DELETE FROM client_endpoint_ip WHERE client_name = ?", (name,))
    conn.execute("DELETE FROM session_baseline WHERE name = ?", (name,))
    conn.commit()


# ---------------- endpoints ----------------

def ensure_endpoint(
    name: str, port: int, protocol: str, cipher: str, mgmt_port: int, tun_dev: str,
    subnet: str, dns1: str, dns2: str, ccd_dir: str,
) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO endpoints "
        "(name, port, protocol, cipher, mgmt_port, tun_dev, subnet, dns1, dns2, ccd_dir, created) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
        (name, port, protocol, cipher, mgmt_port, tun_dev, subnet, dns1, dns2, ccd_dir),
    )
    conn.commit()


def get_endpoint(name: str) -> sqlite3.Row | None:
    conn = get_conn()
    return conn.execute("SELECT * FROM endpoints WHERE name = ?", (name,)).fetchone()


def get_all_endpoints() -> list[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM endpoints ORDER BY created").fetchall()


def delete_endpoint(name: str) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM endpoints WHERE name = ?", (name,))
    conn.execute("DELETE FROM client_endpoint_ip WHERE endpoint_name = ?", (name,))
    conn.execute("DELETE FROM session_baseline WHERE endpoint_name = ?", (name,))
    conn.commit()


def next_mgmt_port() -> int:
    conn = get_conn()
    row = conn.execute("SELECT MAX(mgmt_port) AS m FROM endpoints").fetchone()
    return (row["m"] or 7504) + 1


# ---------------- per-endpoint static IPs ----------------
# Two different reasons a client ends up with a static IP, tracked by `pinned`:
#   - auto-allocated as a side effect of a bandwidth cap (pinned=0, tc needs a stable
#     target address) — ephemeral, cleared when the cap is removed.
#   - explicitly assigned by an admin via client.set_static_ip (pinned=1) — durable,
#     survives bandwidth changes. This is what makes IP-based downstream auth safe: a
#     pinned assignment is never silently dropped by an unrelated limits edit.

def get_client_ips(client_name: str) -> dict[str, str]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT endpoint_name, static_ip FROM client_endpoint_ip WHERE client_name = ?", (client_name,)
    ).fetchall()
    return {r["endpoint_name"]: r["static_ip"] for r in rows}


def get_client_ip_pins(client_name: str) -> dict[str, bool]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT endpoint_name, pinned FROM client_endpoint_ip WHERE client_name = ?", (client_name,)
    ).fetchall()
    return {r["endpoint_name"]: bool(r["pinned"]) for r in rows}


def get_endpoint_used_ips(endpoint_name: str) -> set[str]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT static_ip FROM client_endpoint_ip WHERE endpoint_name = ?", (endpoint_name,)
    ).fetchall()
    return {r["static_ip"] for r in rows}


def get_ip_owner(endpoint_name: str, static_ip: str) -> str | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT client_name FROM client_endpoint_ip WHERE endpoint_name = ? AND static_ip = ?",
        (endpoint_name, static_ip),
    ).fetchone()
    return row["client_name"] if row else None


def set_client_ip(client_name: str, endpoint_name: str, static_ip: str, pinned: bool = False) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO client_endpoint_ip (client_name, endpoint_name, static_ip, pinned) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(client_name, endpoint_name) DO UPDATE SET static_ip = excluded.static_ip, pinned = excluded.pinned",
        (client_name, endpoint_name, static_ip, int(pinned)),
    )
    conn.commit()


def clear_client_ip(client_name: str, endpoint_name: str) -> None:
    """Removes one endpoint's IP assignment for a client regardless of pin
    state — used by the explicit unpin/clear path (client.set_static_ip with
    no static_ip), which is itself an admin action, not an incidental one."""
    conn = get_conn()
    conn.execute(
        "DELETE FROM client_endpoint_ip WHERE client_name = ? AND endpoint_name = ?",
        (client_name, endpoint_name),
    )
    conn.commit()


def clear_unpinned_client_ips(client_name: str) -> None:
    """Called when a bandwidth cap is removed — clears only the IPs that
    were auto-allocated as a side effect of that cap, leaving any admin-
    pinned assignment untouched."""
    conn = get_conn()
    conn.execute("DELETE FROM client_endpoint_ip WHERE client_name = ? AND pinned = 0", (client_name,))
    conn.commit()


def allocate_static_ip(subnet: str, existing_ips: set[str]) -> str:
    network = ipaddress.ip_network(subnet, strict=False)
    hosts = list(network.hosts())
    candidates = hosts[-56:-6] if len(hosts) > 60 else hosts  # reserve a block near the top of the range
    for addr in candidates:
        candidate = str(addr)
        if candidate not in existing_ips:
            return candidate
    raise RuntimeError(f"No free static IPs left in {subnet}.")


# ---------------- usage baselines (per endpoint, since client_id resets per mgmt instance) ----------------

def get_baseline(endpoint_name: str, client_id: str) -> int | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT bytes_seen FROM session_baseline WHERE endpoint_name = ? AND client_id = ?",
        (endpoint_name, client_id),
    ).fetchone()
    return row["bytes_seen"] if row else None


def set_baseline(endpoint_name: str, client_id: str, name: str, bytes_seen: int) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO session_baseline (endpoint_name, client_id, name, bytes_seen) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(endpoint_name, client_id) DO UPDATE SET bytes_seen = excluded.bytes_seen",
        (endpoint_name, client_id, name, bytes_seen),
    )
    conn.commit()


def prune_baselines(endpoint_name: str, active_client_ids: set[str]) -> None:
    conn = get_conn()
    rows = conn.execute(
        "SELECT client_id FROM session_baseline WHERE endpoint_name = ?", (endpoint_name,)
    ).fetchall()
    stale = [r["client_id"] for r in rows if r["client_id"] not in active_client_ids]
    if stale:
        conn.executemany(
            "DELETE FROM session_baseline WHERE endpoint_name = ? AND client_id = ?",
            [(endpoint_name, c) for c in stale],
        )
        conn.commit()
