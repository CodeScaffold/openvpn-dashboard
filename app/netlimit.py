import subprocess
from pathlib import Path

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
    if lines:
        path.write_text("\n".join(lines) + "\n")
    elif path.exists():
        path.unlink()


def remove_ccd(ccd_dir: str, name: str) -> None:
    path = Path(ccd_dir) / name
    if path.exists():
        path.unlink()


def ensure_tc_root(iface: str) -> None:
    show = _tc("qdisc", "show", "dev", iface)
    if "htb 1:" not in show.stdout:
        _tc("qdisc", "add", "dev", iface, "root", "handle", "1:", "htb", "default", "999")
        _tc("class", "add", "dev", iface, "parent", "1:", "classid", "1:999", "htb", "rate", "1000mbit")
    ingress = _tc("qdisc", "show", "dev", iface, "ingress")
    if "ingress" not in ingress.stdout:
        _tc("qdisc", "add", "dev", iface, "handle", "ffff:", "ingress")


def _class_id(static_ip: str) -> int:
    return int(static_ip.rsplit(".", 1)[-1])


def apply_bandwidth(iface: str, static_ip: str, kbps: int) -> None:
    ensure_tc_root(iface)
    cid = _class_id(static_ip)

    # u32 filters aren't reliably idempotent under "replace" (each call can add a new
    # hash-table entry rather than overwrite), so delete any prior filters at this
    # prio before adding fresh ones. Deleting a non-existent prio is a harmless no-op.
    _tc("filter", "del", "dev", iface, "parent", "1:", "prio", str(cid))
    _tc("filter", "del", "dev", iface, "parent", "ffff:", "prio", str(cid))

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


def remove_bandwidth(iface: str, static_ip: str) -> None:
    cid = _class_id(static_ip)
    _tc("filter", "del", "dev", iface, "parent", "1:", "prio", str(cid))
    _tc("filter", "del", "dev", iface, "parent", "ffff:", "prio", str(cid))
    _tc("class", "del", "dev", iface, "parent", "1:", "classid", f"1:{cid}")


def reapply_endpoint(iface: str, ip_to_kbps: dict[str, int]) -> None:
    """Call after an endpoint's openvpn restart, which recreates its tun device and wipes tc state."""
    for static_ip, kbps in ip_to_kbps.items():
        try:
            apply_bandwidth(iface, static_ip, kbps)
        except RuntimeError:
            pass
