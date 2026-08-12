"""Generic schema-versioned migration runner, shared by the agent's
limits.db and the hub's hub.db. Replaces the old ad-hoc pattern of
CREATE TABLE IF NOT EXISTS plus one hand-rolled PRAGMA table_info check."""
import sqlite3
from typing import Callable

Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]


def _ensure_schema_table(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (0)")
        conn.commit()


def current_version(conn: sqlite3.Connection) -> int:
    _ensure_schema_table(conn)
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    return row[0] if row else 0


def apply_migrations(conn: sqlite3.Connection, migrations: list[Migration]) -> None:
    """Apply migrations in ascending version order, skipping versions already
    applied. Each migration commits its own version bump, so a later failure
    never leaves an earlier migration half-recorded."""
    applied = current_version(conn)
    for version, _description, fn in sorted(migrations, key=lambda m: m[0]):
        if version <= applied:
            continue
        fn(conn)
        conn.execute("UPDATE schema_version SET version = ?", (version,))
        conn.commit()
