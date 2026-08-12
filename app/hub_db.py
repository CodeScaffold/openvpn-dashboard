import sqlite3
import threading
import time
from pathlib import Path

from common.migrate import apply_migrations

HUB_DB_PATH = Path("/etc/ovpn-dashboard/hub.db")

_local = threading.local()


def _migration_1(conn: sqlite3.Connection) -> None:
    """Server registry."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS servers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            label TEXT,
            token_hash TEXT NOT NULL,
            public_addr TEXT,
            agent_version TEXT,
            hostname TEXT,
            last_seen TEXT,
            created TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)


def _migration_2(conn: sqlite3.Connection) -> None:
    """The display mirror, fed by telemetry: endpoint/client lists and a
    metrics time series per server (replaces the old single-box in-memory
    ring buffers in system.py — now persisted, per server, on the hub).
    vpn_endpoint is the address THIS server's clients should dial — the old
    single global VPN_ENDPOINT env var, now per-server."""
    conn.execute("ALTER TABLE servers ADD COLUMN vpn_endpoint TEXT")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS server_endpoints (
            server_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            port INTEGER,
            protocol TEXT,
            cipher TEXT,
            dns1 TEXT,
            dns2 TEXT,
            active INTEGER,
            online INTEGER,
            created TEXT,
            updated TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (server_id, name)
        );

        CREATE TABLE IF NOT EXISTS server_clients (
            server_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            team TEXT,
            created TEXT,
            expires TEXT,
            max_connections INTEGER,
            quota_bytes INTEGER,
            used_bytes INTEGER,
            bandwidth_kbps INTEGER,
            disabled INTEGER,
            updated TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (server_id, name)
        );

        CREATE TABLE IF NOT EXISTS metrics (
            server_id INTEGER NOT NULL,
            t REAL NOT NULL,
            cpu_percent REAL,
            mem_used INTEGER,
            mem_total INTEGER,
            swap_used INTEGER,
            swap_total INTEGER,
            disk_used INTEGER,
            disk_total INTEGER,
            net_rx_bps REAL,
            net_tx_bps REAL,
            tcp_count INTEGER,
            udp_count INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_metrics_server_t ON metrics (server_id, t);
    """)


MIGRATIONS = [
    (1, "server registry", _migration_1),
    (2, "display mirror + metrics + vpn_endpoint", _migration_2),
]


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        HUB_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(HUB_DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        apply_migrations(conn, MIGRATIONS)
        _local.conn = conn
    return conn


# ---------------- servers ----------------

def create_server(name: str, label: str, token_hash: str, vpn_endpoint: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO servers (name, label, token_hash, vpn_endpoint) VALUES (?, ?, ?, ?)",
        (name, label, token_hash, vpn_endpoint),
    )
    conn.commit()
    return cur.lastrowid


def get_server(server_id: int) -> sqlite3.Row | None:
    conn = get_conn()
    return conn.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()


def get_server_by_token_hash(token_hash: str) -> sqlite3.Row | None:
    conn = get_conn()
    return conn.execute("SELECT * FROM servers WHERE token_hash = ?", (token_hash,)).fetchone()


def list_servers() -> list[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM servers ORDER BY created").fetchall()


def touch_server(server_id: int, agent_version: str, hostname: str, public_addr: str) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE servers SET agent_version = ?, hostname = ?, public_addr = ?, last_seen = datetime('now') "
        "WHERE id = ?",
        (agent_version, hostname, public_addr, server_id),
    )
    conn.commit()


def touch_last_seen(server_id: int) -> None:
    conn = get_conn()
    conn.execute("UPDATE servers SET last_seen = datetime('now') WHERE id = ?", (server_id,))
    conn.commit()


def update_server(server_id: int, name: str, label: str, vpn_endpoint: str) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE servers SET name = ?, label = ?, vpn_endpoint = ? WHERE id = ?",
        (name, label, vpn_endpoint, server_id),
    )
    conn.commit()


def delete_server(server_id: int) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM servers WHERE id = ?", (server_id,))
    conn.execute("DELETE FROM server_endpoints WHERE server_id = ?", (server_id,))
    conn.execute("DELETE FROM server_clients WHERE server_id = ?", (server_id,))
    conn.execute("DELETE FROM metrics WHERE server_id = ?", (server_id,))
    conn.commit()


# ---------------- display mirror (fed by telemetry) ----------------

def replace_server_endpoints(server_id: int, endpoints: list[dict]) -> None:
    """Full replace, not upsert: the agent's list is authoritative each
    cycle, so a deleted endpoint should disappear from the mirror too."""
    conn = get_conn()
    conn.execute("DELETE FROM server_endpoints WHERE server_id = ?", (server_id,))
    conn.executemany(
        "INSERT INTO server_endpoints "
        "(server_id, name, port, protocol, cipher, dns1, dns2, active, online, created) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (server_id, e["name"], e["port"], e["protocol"], e["cipher"], e.get("dns1"), e.get("dns2"),
             int(bool(e["active"])), e["online"], e["created"])
            for e in endpoints
        ],
    )
    conn.commit()


def get_server_endpoints(server_id: int) -> list[sqlite3.Row]:
    conn = get_conn()
    return conn.execute(
        "SELECT * FROM server_endpoints WHERE server_id = ? ORDER BY created", (server_id,)
    ).fetchall()


def replace_server_clients(server_id: int, clients: list[dict]) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM server_clients WHERE server_id = ?", (server_id,))
    conn.executemany(
        "INSERT INTO server_clients "
        "(server_id, name, team, created, expires, max_connections, quota_bytes, used_bytes, "
        " bandwidth_kbps, disabled) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (server_id, c["name"], c.get("team"), c["created"], c["expires"], c["max_connections"],
             c["quota_bytes"], c["used_bytes"], c["bandwidth_kbps"], int(bool(c["disabled"])))
            for c in clients
        ],
    )
    conn.commit()


def get_server_clients(server_id: int) -> list[sqlite3.Row]:
    conn = get_conn()
    return conn.execute(
        "SELECT * FROM server_clients WHERE server_id = ? ORDER BY name", (server_id,)
    ).fetchall()


def insert_metric_point(server_id: int, snapshot: dict) -> None:
    conn = get_conn()
    mem, swap, disk = snapshot.get("memory", {}), snapshot.get("swap", {}), snapshot.get("disk", {})
    net = snapshot.get("net_totals", {})
    conn.execute(
        "INSERT INTO metrics (server_id, t, cpu_percent, mem_used, mem_total, swap_used, swap_total, "
        "disk_used, disk_total, net_rx_bps, net_tx_bps, tcp_count, udp_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            server_id, time.time(), snapshot.get("cpu_percent", 0.0),
            mem.get("used", 0), mem.get("total", 0), swap.get("used", 0), swap.get("total", 0),
            disk.get("used", 0), disk.get("total", 0),
            snapshot.get("net_rx_bps", 0.0), snapshot.get("net_tx_bps", 0.0),
            snapshot.get("tcp_count", 0), snapshot.get("udp_count", 0),
        ),
    )
    conn.commit()


def get_metrics(server_id: int, limit: int) -> list[sqlite3.Row]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM metrics WHERE server_id = ? ORDER BY t DESC LIMIT ?", (server_id, limit)
    ).fetchall()
    return list(reversed(rows))


def prune_metrics(server_id: int, keep: int) -> None:
    conn = get_conn()
    conn.execute(
        "DELETE FROM metrics WHERE server_id = ? AND t NOT IN "
        "(SELECT t FROM metrics WHERE server_id = ? ORDER BY t DESC LIMIT ?)",
        (server_id, server_id, keep),
    )
    conn.commit()
