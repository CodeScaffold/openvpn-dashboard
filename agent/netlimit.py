import subprocess
from pathlib import Path

from . import db, ipv6

STATIC_NETMASK = "255.255.255.0"


def _tc(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(["tc", *args], capture_output=True, text=True, timeout=10)
    if check and result.returncode != 0:
        raise RuntimeError(f"tc {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def write_ccd(ccd_dir: str, name: str, static_ip: str | None, disabled: bool) -> None:
    Path(ccd_dir).mkdir(parents=True, exist_ok=True)
    path = Path(ccd_dir) / name
    lines = []
    if disabled:
        lines.append("disable")
    if static_ip:
        lines.append(f"ifconfig-push {static_ip} {STATIC_NETMASK}")
        from . import endpoints
        subnet = ipv6.configured_network(endpoints.SERVER_DIR / f"{Path(ccd_dir).name}.conf")
        if subnet:
            lines.append(f"ifconfig-ipv6-push {ipv6.client_address(subnet, static_ip)}/{subnet.prefixlen} {subnet.network_address + 1}")
    if lines:
        path.write_text("\n".join(lines) + "\n")
    elif path.exists():
        path.unlink()


def remove_ccd(ccd_dir: str, name: str) -> None:
    path = Path(ccd_dir) / name
    if path.exists():
        path.unlink()


def has_tc_root(iface: str) -> bool:
    return "htb 1:" in _tc("qdisc", "show", "dev", iface).stdout


def ensure_tc_root(iface: str) -> None:
    if not has_tc_root(iface):
        _tc("qdisc", "add", "dev", iface, "root", "handle", "1:", "htb", "default", "999")
        _tc("class", "add", "dev", iface, "parent", "1:", "classid", "1:999", "htb", "rate", "1000mbit")
    ingress = _tc("qdisc", "show", "dev", iface, "ingress")
    if "ingress" not in ingress.stdout:
        _tc("qdisc", "add", "dev", iface, "handle", "ffff:", "ingress")


def _class_id(static_ip: str) -> int:
    return int(static_ip.rsplit(".", 1)[-1])


def _ipv6_address(iface: str, static_ip: str) -> str | None:
    from . import endpoints
    for row in db.get_all_endpoints():
        if row["tun_dev"] == iface:
            subnet = ipv6.configured_network(endpoints.SERVER_DIR / f"{row['name']}.conf")
            return ipv6.client_address(subnet, static_ip) if subnet else None
    return None


def apply_bandwidth(iface: str, static_ip: str, kbps: int) -> None:
    ensure_tc_root(iface)
    cid = _class_id(static_ip)

    # u32 filters aren't reliably idempotent under "replace" (each call can add a new
    # hash-table entry rather than overwrite), so delete any prior filters at this
    # prio before adding fresh ones. Deleting a non-existent prio is a harmless no-op.
    _tc("filter", "del", "dev", iface, "parent", "1:", "prio", str(cid))
    _tc("filter", "del", "dev", iface, "parent", "ffff:", "prio", str(cid))
    _tc("filter", "del", "dev", iface, "parent", "1:", "prio", str(cid + 256))
    _tc("filter", "del", "dev", iface, "parent", "ffff:", "prio", str(cid + 256))

    _tc(
        "class", "replace", "dev", iface, "parent", "1:", "classid", f"1:{cid}",
        "htb", "rate", f"{kbps}kbit", "ceil", f"{kbps}kbit",
    )
    _tc(
        "filter", "add", "dev", iface, "protocol", "ip", "parent", "1:", "prio", str(cid),
        "u32", "match", "ip", "dst", f"{static_ip}/32", "flowid", f"1:{cid}",
    )
    _tc(
        "filter", "add", "dev", iface, "protocol", "ip", "parent", "ffff:", "prio", str(cid),
        "u32", "match", "ip", "src", f"{static_ip}/32",
        "police", "rate", f"{kbps}kbit", "burst", "32k", "drop", "flowid", ":1",
    )
    address6 = _ipv6_address(iface, static_ip)
    if address6:
        _tc(
            "filter", "add", "dev", iface, "protocol", "ipv6", "parent", "1:", "prio", str(cid + 256),
            "u32", "match", "ip6", "dst", f"{address6}/128", "flowid", f"1:{cid}",
            check=True,
        )
        _tc(
            "filter", "add", "dev", iface, "protocol", "ipv6", "parent", "ffff:", "prio", str(cid + 256),
            "u32", "match", "ip6", "src", f"{address6}/128",
            "police", "rate", f"{kbps}kbit", "burst", "32k", "drop", "flowid", ":1",
            check=True,
        )


def remove_bandwidth(iface: str, static_ip: str) -> None:
    cid = _class_id(static_ip)
    _tc("filter", "del", "dev", iface, "parent", "1:", "prio", str(cid))
    _tc("filter", "del", "dev", iface, "parent", "ffff:", "prio", str(cid))
    _tc("filter", "del", "dev", iface, "parent", "1:", "prio", str(cid + 256))
    _tc("filter", "del", "dev", iface, "parent", "ffff:", "prio", str(cid + 256))
    _tc("class", "del", "dev", iface, "parent", "1:", "classid", f"1:{cid}")


def reapply_endpoint(iface: str, ip_to_kbps: dict[str, int]) -> None:
    """Call after an endpoint's openvpn restart, which recreates its tun device and wipes tc state."""
    for static_ip, kbps in ip_to_kbps.items():
        try:
            apply_bandwidth(iface, static_ip, kbps)
        except RuntimeError:
            pass
