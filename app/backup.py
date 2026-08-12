import tarfile
from pathlib import Path

ADMIN_FILE = Path("/etc/ovpn-dashboard/admin.json")
BACKUP_DIR = Path("/etc/ovpn-dashboard/backups")

# Per-server backups (PKI, ccd, server confs, limits.db) live with the agent —
# see agent/backup.py. This module only ever handles dashboard-side state.
MEMBER_ROOTS = {
    "admin": ADMIN_FILE.parent,
}


def build_backup_tar(dest_path: Path) -> None:
    """Bundles the dashboard's own state (admin.json — settings + password
    hash) into a tar.gz. Does not include any server's PKI or client data;
    those are requested from the relevant agent separately."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest_path, "w:gz") as tar:
        if ADMIN_FILE.exists():
            tar.add(ADMIN_FILE, arcname=f"admin/{ADMIN_FILE.name}")


def _safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    safe = []
    for member in tar.getmembers():
        parts = Path(member.name).parts
        if not parts or parts[0] not in MEMBER_ROOTS:
            continue
        if ".." in parts:
            continue
        if member.issym() or member.islnk():
            continue
        safe.append(member)
    return safe


def restore_from_tar(src_path: Path) -> None:
    """Extracts a dashboard-state backup archive over the live admin.json."""
    with tarfile.open(src_path, "r:gz") as tar:
        members = _safe_members(tar)
        if not members:
            raise ValueError("Archive has no recognizable dashboard backup contents.")
        for member in members:
            parts = Path(member.name).parts
            dest_root = MEMBER_ROOTS[parts[0]]
            rel_parts = parts[1:]
            target = dest_root.joinpath(*rel_parts) if rel_parts else dest_root
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            f = tar.extractfile(member)
            if f is None:
                continue
            target.write_bytes(f.read())
    if ADMIN_FILE.exists():
        ADMIN_FILE.chmod(0o600)
