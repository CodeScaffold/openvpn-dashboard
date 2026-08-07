import ipaddress
import re
import subprocess
import time
from pathlib import Path

from . import db

SERVER_DIR = Path("/etc/openvpn/server")
NAME_RE = re.compile(r"^[a-z0-9-]{1,24}$")


def is_port_free(port: int, protocol: str) -> bool:
    flag = "-tln" if protocol == "tcp" else "-uln"
    result = subprocess.run(["ss", flag], capture_output=True, text=True, timeout=5)
    return f":{port} " not in result.stdout and not re.search(rf":{port}\b", result.stdout)


def _config_text(name: str, port: int, protocol: str, cipher: str, tun_dev: str,
                  subnet: str, dns1: str, dns2: str, mgmt_port: int, ccd_dir: str) -> str:
    network = ipaddress.ip_network(subnet, strict=False)
    lines = [
        f"port {port}",
        f"proto {protocol}",
        f"dev {tun_dev}",
        "ca ca.crt",
        "cert server.crt",
        "key server.key",
        "tls-crypt tls-crypt.key",
        "dh none",
        "ecdh-curve prime256v1",
        "topology subnet",
        f"server {network.network_address} {network.netmask}",
        f"ifconfig-pool-persist ipp-{name}.txt",
        'push "redirect-gateway def1 bypass-dhcp"',
    ]
    if dns1:
        lines.append(f'push "dhcp-option DNS {dns1}"')
    if dns2:
        lines.append(f'push "dhcp-option DNS {dns2}"')
    lines += [
        "keepalive 10 120",
        f"cipher {cipher}",
        f"data-ciphers {cipher}",
        "auth SHA256",
        "user nobody",
        "group nogroup",
        "persist-key",
        "persist-tun",
        f"status /var/log/openvpn/status-{name}.log",
        "verb 3",
    ]
    if protocol == "udp":
        lines.append("explicit-exit-notify 1")
    lines += [
        "crl-verify crl.pem",
        f"management 127.0.0.1 {mgmt_port}",
        f"client-config-dir {ccd_dir}",
        "duplicate-cn",
    ]
    return "\n".join(lines) + "\n"


def create_endpoint(name: str, port: int, protocol: str, cipher: str, dns1: str, dns2: str) -> None:
    if not NAME_RE.match(name):
        raise ValueError("Endpoint name must be lowercase letters, numbers, and dashes only (max 24 chars).")
    if db.get_endpoint(name):
        raise ValueError(f'An endpoint named "{name}" already exists.')
    if protocol not in ("udp", "tcp"):
        raise ValueError("protocol must be udp or tcp.")
    if not (1 <= port <= 65535):
        raise ValueError("Invalid port.")
    if not is_port_free(port, protocol):
        raise ValueError(f"Port {port}/{protocol} is already in use.")

    existing = db.get_all_endpoints()
    index = len(existing)
    for row in existing:
        idx = int(row["tun_dev"].replace("tun", ""))
        index = max(index, idx + 1)

    mgmt_port = db.next_mgmt_port()
    tun_dev = f"tun{index}"
    subnet = f"10.9.{index}.0/24"
    ccd_dir = f"/etc/openvpn/ccd/{name}"

    Path(ccd_dir).mkdir(parents=True, exist_ok=True)
    subprocess.run(["chown", "ovpndash:ovpndash", ccd_dir], check=False)

    conf_path = SERVER_DIR / f"{name}.conf"
    conf_path.write_text(
        _config_text(name, port, protocol, cipher, tun_dev, subnet, dns1, dns2, mgmt_port, ccd_dir)
    )

    subprocess.run(["sudo", "-n", "ufw", "allow", f"{port}/{protocol}", "comment", f"ovpn-{name}"],
                    capture_output=True, text=True)

    result = subprocess.run(
        ["sudo", "-n", "systemctl", "enable", "--now", f"openvpn-server@{name}"],
        capture_output=True, text=True,
    )
    time.sleep(2)
    active = subprocess.run(
        ["systemctl", "is-active", f"openvpn-server@{name}"], capture_output=True, text=True
    ).stdout.strip()

    if active != "active":
        conf_path.unlink(missing_ok=True)
        subprocess.run(["sudo", "-n", "systemctl", "disable", f"openvpn-server@{name}"],
                        capture_output=True, text=True)
        subprocess.run(["sudo", "-n", "ufw", "delete", "allow", f"{port}/{protocol}"],
                        capture_output=True, text=True)
        raise RuntimeError(f"OpenVPN failed to start for endpoint '{name}': {result.stderr.strip()[-400:]}")

    db.ensure_endpoint(name, port, protocol, cipher, mgmt_port, tun_dev, subnet, dns1, dns2, ccd_dir)


def delete_endpoint(name: str) -> None:
    row = db.get_endpoint(name)
    if not row:
        raise ValueError(f'No endpoint named "{name}".')
    if name == "server":
        raise ValueError('The original "server" endpoint cannot be deleted.')

    subprocess.run(["sudo", "-n", "systemctl", "disable", "--now", f"openvpn-server@{name}"],
                    capture_output=True, text=True)
    (SERVER_DIR / f"{name}.conf").unlink(missing_ok=True)
    subprocess.run(["sudo", "-n", "ufw", "delete", "allow", f"{row['port']}/{row['protocol']}"],
                    capture_output=True, text=True)

    ccd_dir = Path(row["ccd_dir"])
    if ccd_dir.exists():
        for f in ccd_dir.iterdir():
            f.unlink()
        ccd_dir.rmdir()

    db.delete_endpoint(name)


def is_active(name: str) -> bool:
    result = subprocess.run(
        ["systemctl", "is-active", f"openvpn-server@{name}"], capture_output=True, text=True
    )
    return result.stdout.strip() == "active"
