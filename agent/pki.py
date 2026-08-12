import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import db

EASYRSA_DIR = Path("/etc/openvpn/easy-rsa")
SERVER_CRL = Path("/etc/openvpn/server/crl.pem")
RESERVED_NAMES = {"server"}


def run_easyrsa(*args: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "EASYRSA_BATCH": "1", **(extra_env or {})}
    result = subprocess.run(
        ["./easyrsa", *args], cwd=EASYRSA_DIR, env=env, capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"easyrsa {' '.join(args)} failed: {result.stderr.strip()[-800:]}")
    return result


def read_client_index() -> dict[str, dict]:
    """Parses easy-rsa's pki/index.txt once: {name: {"status": "V"|"R", "expires": iso|None}}."""
    index = EASYRSA_DIR / "pki" / "index.txt"
    if not index.exists():
        return {}
    out: dict[str, dict] = {}
    for line in index.read_text().splitlines():
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        status, expiry_raw = parts[0], parts[1]
        m = re.search(r"/CN=([^/]+)", parts[5])
        if not m:
            continue
        cn = m.group(1)
        if cn in RESERVED_NAMES:
            continue
        expires_iso = None
        try:
            expires_iso = datetime.strptime(expiry_raw, "%y%m%d%H%M%SZ").replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass
        out[cn] = {"status": status, "expires": expires_iso}
    return out


def read_valid_client_names() -> list[str]:
    return [name for name, info in read_client_index().items() if info["status"] == "V"]


def client_created_at(name: str) -> str:
    cert = EASYRSA_DIR / "pki" / "issued" / f"{name}.crt"
    if not cert.exists():
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(cert.stat().st_mtime, tz=timezone.utc).isoformat()


def build_ovpn(name: str, endpoint_name: str, vpn_endpoint: str) -> str:
    """vpn_endpoint is the address clients should dial for this server — the
    hub supplies it per-server (was a single global VPN_ENDPOINT env var)."""
    ep = db.get_endpoint(endpoint_name)
    if not ep:
        raise ValueError(f'No endpoint named "{endpoint_name}".')
    pki = EASYRSA_DIR / "pki"
    ca = (pki / "ca.crt").read_text()
    cert_raw = (pki / "issued" / f"{name}.crt").read_text()
    cert = cert_raw[cert_raw.index("-----BEGIN CERTIFICATE-----"):]
    key = (pki / "private" / f"{name}.key").read_text()
    tls_crypt = (pki / "tls-crypt.key").read_text()

    return f"""client
dev tun
proto {ep['protocol']}
remote {vpn_endpoint} {ep['port']}
resolv-retry infinite
nobind
persist-key
persist-tun
remote-cert-tls server
cipher {ep['cipher']}
data-ciphers {ep['cipher']}
auth SHA256
verb 3
<ca>
{ca.strip()}
</ca>
<cert>
{cert.strip()}
</cert>
<key>
{key.strip()}
</key>
<tls-crypt>
{tls_crypt.strip()}
</tls-crypt>
"""


def refresh_crl() -> None:
    run_easyrsa("gen-crl")
    crl_src = EASYRSA_DIR / "pki" / "crl.pem"
    SERVER_CRL.write_text(crl_src.read_text())
    SERVER_CRL.chmod(0o644)
