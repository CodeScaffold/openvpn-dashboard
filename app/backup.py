import logging
import os
import subprocess
import tarfile
from pathlib import Path

from . import db

log = logging.getLogger("ovpn-dashboard.backup")

EASYRSA_PKI = Path("/etc/openvpn/easy-rsa/pki")
CCD_ROOT = Path("/etc/openvpn/ccd")
SERVER_DIR = Path("/etc/openvpn/server")
DB_FILE = db.DB_PATH
ADMIN_FILE = Path("/etc/ovpn-dashboard/admin.json")
BACKUP_DIR = Path("/etc/ovpn-dashboard/backups")

# Top-level archive roots a restore is allowed to touch. Anything else found
# inside an uploaded archive is silently ignored (defends against path
# traversal / zip-slip via crafted member names).
MEMBER_ROOTS = {
    "pki": EASYRSA_PKI,
    "ccd": CCD_ROOT,
    "server-conf": SERVER_DIR,
    "db": DB_FILE.parent,
    "admin": ADMIN_FILE.parent,
}

SKIP_SUFFIXES = (".log",)


def _add_file(tar: tarfile.TarFile, path: Path, arcname: str) -> None:
    # a stray root-owned file (e.g. an openvpn-written ipp-*.txt before it drops
    # privileges) must not abort the whole backup — skip it and keep going.
    try:
        tar.add(path, arcname=arcname)
    except (PermissionError, OSError) as e:
        log.warning("backup: skipping unreadable %s: %s", path, e)


def _add_tree(tar: tarfile.TarFile, src: Path, arcname: str) -> None:
    if not src.exists():
        return
    if src.is_file():
        _add_file(tar, src, arcname)
        return
    for root, _dirs, files in os.walk(src):
        rel_root = Path(root).relative_to(src)
        for name in files:
            if any(name.endswith(suf) for suf in SKIP_SUFFIXES):
                continue
            fpath = Path(root) / name
            arc = f"{arcname}/{rel_root / name}" if str(rel_root) != "." else f"{arcname}/{name}"
            _add_file(tar, fpath, arc)


def build_backup_tar(dest_path: Path) -> None:
    """Bundles PKI, per-endpoint configs, ccd, the sqlite db and admin.json
    into a self-contained tar.gz. Contains the CA private key — treat the
    result like a root credential."""
    conn = db.get_conn()
    conn.execute("PRAGMA wal_checkpoint(FULL)")

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest_path, "w:gz") as tar:
        _add_tree(tar, EASYRSA_PKI, "pki")
        _add_tree(tar, CCD_ROOT, "ccd")
        _add_tree(tar, SERVER_DIR, "server-conf")
        if DB_FILE.exists():
            tar.add(DB_FILE, arcname=f"db/{DB_FILE.name}")
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


def restore_from_tar(src_path: Path) -> list[str]:
    """Extracts a backup archive over the live config. Returns the endpoint
    names found in the restored server-conf/, for the caller to restart."""
    with tarfile.open(src_path, "r:gz") as tar:
        members = _safe_members(tar)
        if not members:
            raise ValueError("Archive has no recognizable ovpn-dashboard backup contents.")
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

    subprocess.run(["chown", "-R", "ovpndash:ovpndash", str(CCD_ROOT)], check=False)
    if ADMIN_FILE.exists():
        ADMIN_FILE.chmod(0o600)

    return sorted(p.stem for p in SERVER_DIR.glob("*.conf"))
