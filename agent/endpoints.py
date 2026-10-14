import ipaddress
import os
import re
import subprocess
import time
from pathlib import Path

from . import db, ipv6

SERVER_DIR = Path("/etc/openvpn/server")
NAME_RE = re.compile(r"^[a-z0-9-]{1,24}$")
# Narrow wrappers the sudoers grants point at instead of wildcard patterns —
# some sudo implementations (e.g. sudo-rs, default on newer Ubuntu) reject wildcards
# embedded within a single argument. Each wrapper's own validation must stay in sync
# with the constants below.
ENDPOINT_CTL = "/usr/local/bin/ovpn-endpoint-ctl"
NAT_CTL = "/usr/local/bin/ovpn-nat-ctl"
BIND_IP_CTL = "/usr/local/bin/ovpn-bind-ip-ctl"
FW_CTL = "/usr/local/bin/ovpn-fw-ctl"

# Set when this agent shares a box with other agent instances, each pinned to its own
# IP (e.g. Hetzner floating IPs) -- see README "Multiple exit IPs on one box". When set,
# every endpoint this agent creates binds OpenVPN to exactly this address and NATs its
# clients' traffic out through it, instead of the box's default address. Unset (the
# common case, one agent per box) preserves the original single-IP behaviour untouched.
BIND_IP = os.environ.get("BIND_IP") or None


def _default_iface() -> str:
    result = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True, timeout=5)
    for line in result.stdout.splitlines():
        parts = line.split()
        if "dev" in parts:
            return parts[parts.index("dev") + 1]
    return "eth0"


def is_port_free(port: int, protocol: str, bind_ip: str | None = None) -> bool:
    """Ignores sockets bound to a different specific IP than ours -- multiple agent
    instances sharing one box can each hold the same port on their own address (this is
    exactly what makes putting several endpoints on 443 across floating IPs work). A
    wildcard bind (0.0.0.0/::), ours or anyone else's, still blocks the whole port."""
    flag = "-tln" if protocol == "tcp" else "-uln"
    result = subprocess.run(["ss", flag], capture_output=True, text=True, timeout=5)
    for line in result.stdout.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 4:
            continue
        local_addr = cols[3]
        addr, _, p = local_addr.rpartition(":")
        if p != str(port):
            continue
        addr = addr.strip("[]")
        if addr in ("0.0.0.0", "*", "::"):
            return False
        if bind_ip is None or addr == bind_ip:
            return False
    return True


def _next_mgmt_port() -> int:
    """Host-wide, same reasoning as _next_tun_index -- the management socket always
    binds 127.0.0.1, which several agent instances sharing one box (BIND_IP) all see
    as the same loopback. Each agent's own DB has no visibility into what mgmt port
    another instance already grabbed, so probe the live host instead of trusting
    db.next_mgmt_port() blindly."""
    candidate = db.next_mgmt_port()
    while not is_port_free(candidate, "tcp", "127.0.0.1"):
        candidate += 1
    return candidate


def _next_tun_index() -> int:
    """Host-wide, not just this agent's own DB -- when several agent instances share a
    box (see BIND_IP above), each starting from its own empty DB would happily pick the
    same tun0 the others already hold. Checking live interfaces avoids that collision."""
    existing = db.get_all_endpoints()
    index = len(existing)
    for row in existing:
        index = max(index, int(row["tun_dev"].replace("tun", "")) + 1)

    result = subprocess.run(["ip", "-o", "link", "show"], capture_output=True, text=True, timeout=5)
    for m in re.finditer(r"\btun(\d+)\b", result.stdout):
        index = max(index, int(m.group(1)) + 1)
    return index


def _open_port(port: int, protocol: str) -> None:
    subprocess.run(["sudo", "-n", FW_CTL, "add", str(port), protocol], capture_output=True, text=True)


def _close_port(port: int, protocol: str) -> None:
    subprocess.run(["sudo", "-n", FW_CTL, "remove", str(port), protocol], capture_output=True, text=True)


def _ensure_nat(subnet: str) -> None:
    if BIND_IP:
        args = [NAT_CTL, "add", subnet, "snat", BIND_IP]
    else:
        args = [NAT_CTL, "add", subnet, "masquerade"]
    subprocess.run(["sudo", "-n", *args], capture_output=True, text=True)


def _remove_nat(subnet: str) -> None:
    if BIND_IP:
        args = [NAT_CTL, "remove", subnet, "snat", BIND_IP]
    else:
        args = [NAT_CTL, "remove", subnet, "masquerade"]
    subprocess.run(["sudo", "-n", *args], capture_output=True, text=True)


def _config_text(name: str, port: int, protocol: str, cipher: str, tun_dev: str,
                  subnet: str, dns1: str, dns2: str, mgmt_port: int, ccd_dir: str) -> str:
    network = ipaddress.ip_network(subnet, strict=False)
    lines = [
        f"port {port}",
        f"proto {protocol}",
        f"dev {tun_dev}",
    ]
    if BIND_IP:
        lines.append(f"local {BIND_IP}")
    lines += [
        "ca ca.crt",
        "cert server.crt",
        "key server.key",
        "tls-crypt tls-crypt.key",
        "dh none",
        "ecdh-curve prime256v1",
        "topology subnet",
        f"server {network.network_address} {network.netmask}",
        f"ifconfig-pool-persist ipp-{name}.txt",
        'push "redirect-gateway def1 bypass-dhcp ipv6"',
    ]
    lines += ipv6.config_lines(subnet)
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
    if not is_port_free(port, protocol, BIND_IP):
        raise ValueError(f"Port {port}/{protocol} is already in use.")

    index = _next_tun_index()
    mgmt_port = _next_mgmt_port()
    tun_dev = f"tun{index}"
    subnet = f"10.9.{index}.0/24"
    ccd_dir = f"/etc/openvpn/ccd/{name}"

    Path(ccd_dir).mkdir(parents=True, exist_ok=True)
    subprocess.run(["chown", "ovpndash:ovpndash", ccd_dir], check=False)

    conf_path = SERVER_DIR / f"{name}.conf"
    conf_path.write_text(
        _config_text(name, port, protocol, cipher, tun_dev, subnet, dns1, dns2, mgmt_port, ccd_dir)
    )

    _open_port(port, protocol)
    _ensure_nat(subnet)
    try:
        ipv6.configure_runtime("add", tun_dev, ipv6.network(subnet))
    except Exception:
        conf_path.unlink(missing_ok=True)
        _close_port(port, protocol)
        _remove_nat(subnet)
        raise

    result = subprocess.run(
        ["sudo", "-n", ENDPOINT_CTL, "enable-now", name],
        capture_output=True, text=True,
    )
    time.sleep(2)
    active = subprocess.run(
        ["systemctl", "is-active", f"openvpn-server@{name}"], capture_output=True, text=True
    ).stdout.strip()

    if active != "active":
        conf_path.unlink(missing_ok=True)
        subprocess.run(["sudo", "-n", ENDPOINT_CTL, "disable", name],
                        capture_output=True, text=True)
        _close_port(port, protocol)
        _remove_nat(subnet)
        ipv6.configure_runtime("remove", tun_dev, ipv6.network(subnet))
        raise RuntimeError(f"OpenVPN failed to start for endpoint '{name}': {result.stderr.strip()[-400:]}")

    db.ensure_endpoint(name, port, protocol, cipher, mgmt_port, tun_dev, subnet, dns1, dns2, ccd_dir)


def delete_endpoint(name: str) -> None:
    row = db.get_endpoint(name)
    if not row:
        raise ValueError(f'No endpoint named "{name}".')

    subprocess.run(["sudo", "-n", ENDPOINT_CTL, "disable-now", name],
                    capture_output=True, text=True)
    conf_path = SERVER_DIR / f"{name}.conf"
    ipv6_subnet = ipv6.configured_network(conf_path)
    if ipv6_subnet:
        ipv6.configure_runtime("remove", row["tun_dev"], ipv6_subnet)
    conf_path.unlink(missing_ok=True)
    _close_port(row["port"], row["protocol"])
    _remove_nat(row["subnet"])

    ccd_dir = Path(row["ccd_dir"])
    if ccd_dir.exists():
        for f in ccd_dir.iterdir():
            f.unlink()
        ccd_dir.rmdir()

    db.delete_endpoint(name)


def reconcile_nat() -> None:
    """Called once at agent startup. A ufw reload or a reboot only reapplies what's
    persisted in ufw's own config, which our per-endpoint rules deliberately aren't
    part of -- so every endpoint this agent already owns gets its rule re-asserted
    here. _ensure_nat is idempotent (checks with -C before adding), so this is cheap
    even when nothing actually needs fixing."""
    for row in db.get_all_endpoints():
        _ensure_nat(row["subnet"])


def reconcile_ports() -> None:
    """Called once at agent startup, same reasoning as reconcile_nat -- a reboot clears
    the raw iptables rule ovpn-fw-ctl adds, so every endpoint's port gets re-opened."""
    for row in db.get_all_endpoints():
        _open_port(row["port"], row["protocol"])


def reconcile_endpoints() -> None:
    """Called once at agent startup, alongside reconcile_nat. The endpoint's config
    file lives on a persistent volume/disk and survives fine, but systemd's "this
    unit is enabled" symlink does not survive a Docker container being recreated
    (a fresh container gets a fresh root filesystem layer) -- so a real, already
    -configured endpoint can end up silently stopped after something as ordinary as
    an image update, until this notices and starts it back up."""
    for row in db.get_all_endpoints():
        name = row["name"]
        active = subprocess.run(
            ["systemctl", "is-active", f"openvpn-server@{name}"], capture_output=True, text=True
        ).stdout.strip()
        if active != "active":
            subprocess.run(["sudo", "-n", ENDPOINT_CTL, "enable-now", name], capture_output=True, text=True)


def ensure_bind_ip() -> None:
    """Self-heal, called periodically from the agent's telemetry loop, not just at
    startup. The persistent fix for BIND_IP is adding it to the box's own network
    config (netplan/etc -- see README); this exists because a box that manages its
    interface via DHCP can still silently drop an address that was only ever added
    imperatively (a lease renewal reconciles the interface back to its declared
    config), and that can happen hours after the agent last started. ovpn-bind-ip-ctl
    is idempotent, so calling this often is cheap."""
    if not BIND_IP:
        return
    subprocess.run(["sudo", "-n", BIND_IP_CTL, BIND_IP], capture_output=True, text=True)


def is_active(name: str) -> bool:
    result = subprocess.run(
        ["systemctl", "is-active", f"openvpn-server@{name}"], capture_output=True, text=True
    )
    return result.stdout.strip() == "active"
