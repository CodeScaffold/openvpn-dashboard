"""IPv6 tunnel addressing and explicit, per-agent IPv6 egress configuration.

IPV6_EGRESS enables NAT66 through an IPv6 address already assigned to the host.
Without it, generated endpoints route IPv6 into OpenVPN and reject it, rather
than allowing a client's local IPv6 connection to bypass an IPv4-only VPN.
Existing configs are upgraded explicitly; starting an agent never restarts VPNs.
"""
import argparse
import hashlib
import ipaddress
import os
import re
import subprocess
from pathlib import Path

CTL = "/usr/local/bin/ovpn-ipv6-ctl"
BEGIN = "# BEGIN ovpn-dashboard IPv6"
END = "# END ovpn-dashboard IPv6"


def egress() -> str | None:
    value = os.environ.get("IPV6_EGRESS", "").strip()
    if not value:
        return None
    address = ipaddress.IPv6Address(value)
    if not address.is_global or address.ipv4_mapped:
        raise ValueError("IPV6_EGRESS must be a public IPv6 address assigned to this host.")
    return str(address)


def network(subnet: str) -> ipaddress.IPv6Network:
    # Distinct /64s for endpoints and agents sharing a host via BIND_IP.
    identity = f"{os.environ.get('BIND_IP', '')}:{ipaddress.IPv4Network(subnet)}"
    prefix = b"\xfd" + hashlib.sha256(identity.encode()).digest()[:7]
    return ipaddress.IPv6Network((int.from_bytes(prefix + bytes(8), "big"), 64))


def config_lines(subnet: str) -> list[str]:
    lines = [BEGIN, f"server-ipv6 {network(subnet)}", END]
    if not egress():
        lines.insert(-1, "block-ipv6")
    return lines


def configured_network(path: Path) -> ipaddress.IPv6Network | None:
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        parts = line.split()
        if parts and parts[0] == "server-ipv6" and len(parts) == 2:
            return ipaddress.IPv6Network(parts[1])
    return None


def client_address(subnet: ipaddress.IPv6Network, ipv4: str) -> str:
    # Reserve addresses below OpenVPN's dynamic pool (::1000 onward). Static
    # IPv4 CCD assignments disable automatic IPv6 pool allocation in OpenVPN,
    # so each static IPv4 also needs an explicit IPv6 CCD assignment.
    return str(subnet.network_address + 0x100 + int(ipaddress.IPv4Address(ipv4)) % 256)


def configure_runtime(action: str, iface: str, subnet: ipaddress.IPv6Network) -> None:
    target = egress()
    if not target:
        return
    result = subprocess.run(
        ["sudo", "-n", CTL, action, iface, str(subnet), target],
        capture_output=True, text=True, timeout=20,
    )
    if result.returncode:
        raise RuntimeError(f"IPv6 {action} failed: {result.stderr.strip()[-400:]}")


def reconcile() -> None:
    from . import db, endpoints
    for row in db.get_all_endpoints():
        path = endpoints.SERVER_DIR / f"{row['name']}.conf"
        subnet = configured_network(path)
        if subnet and "block-ipv6" not in path.read_text().splitlines():
            configure_runtime("add", row["tun_dev"], subnet)


def upgrade_config(text: str, subnet: str) -> str:
    """Preserve custom endpoint settings; only own the marked IPv6 block."""
    text = re.sub(r"(?m)^" + re.escape(BEGIN) + r"\n.*?^" + re.escape(END) + r"\n?", "", text, flags=re.S)
    if re.search(r"(?m)^\s*(server-ipv6|ifconfig-ipv6|block-ipv6)\b", text):
        raise ValueError("Endpoint has custom IPv6 settings; review it before upgrading.")
    def redirect(match: re.Match) -> str:
        flags = match[1].split()
        if "!ipv4" in flags:
            raise ValueError("Endpoint has custom IPv6-only routing; review it before upgrading.")
        if "ipv6" not in flags:
            flags.append("ipv6")
        return 'push "redirect-gateway ' + " ".join(flags) + '"'
    text, count = re.subn(r'(?m)^push "redirect-gateway ([^"\n]+)"$', redirect, text)
    if count != 1:
        raise ValueError("Expected one redirect-gateway push; review custom routing before upgrading.")
    return text.rstrip() + "\n" + "\n".join(config_lines(subnet)) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare existing endpoints for IPv6; does not restart services.")
    parser.add_argument("--upgrade-configs", action="store_true", required=True)
    args = parser.parse_args()
    from . import db, endpoints, netlimit
    # Validate every config before writing any of them. Keep an adjacent original.
    updates = []
    for row in db.get_all_endpoints():
        path = endpoints.SERVER_DIR / f"{row['name']}.conf"
        updates.append((row, path, upgrade_config(path.read_text(), row["subnet"])))
    for row, path, content in updates:
        backup = path.with_suffix(".conf.pre-ipv6")
        if not backup.exists():
            backup.write_text(path.read_text())
        path.write_text(content)
        print(f"Prepared {row['name']}; restart its OpenVPN service to activate.")
        for name, limits in db.get_all_limits().items():
            ip = db.get_client_ips(name).get(row["name"])
            if ip:
                netlimit.write_ccd(row["ccd_dir"], name, ip, bool(limits["disabled"]))
    reconcile()


if __name__ == "__main__":
    main()
