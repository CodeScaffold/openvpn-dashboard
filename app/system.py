import shutil
import time
from collections import deque

IFACE = "eth0"
HISTORY_LEN = 60

_net_history: deque = deque(maxlen=HISTORY_LEN)
_last_net: tuple[float, int, int] | None = None

_cpu_history: deque = deque(maxlen=HISTORY_LEN)
_mem_history: deque = deque(maxlen=HISTORY_LEN)
_swap_history: deque = deque(maxlen=HISTORY_LEN)
_disk_history: deque = deque(maxlen=HISTORY_LEN)
_conn_history: deque = deque(maxlen=HISTORY_LEN)
_last_cpu: tuple[int, int] | None = None  # (idle, total) jiffies


def set_retention(points: int) -> None:
    """Resize every history ring buffer, keeping as much of the existing
    tail as fits. deque.maxlen is immutable, so buffers are recreated."""
    global HISTORY_LEN, _net_history, _cpu_history, _mem_history, _swap_history, _disk_history, _conn_history
    HISTORY_LEN = points
    _net_history = deque(_net_history, maxlen=points)
    _cpu_history = deque(_cpu_history, maxlen=points)
    _mem_history = deque(_mem_history, maxlen=points)
    _swap_history = deque(_swap_history, maxlen=points)
    _disk_history = deque(_disk_history, maxlen=points)
    _conn_history = deque(_conn_history, maxlen=points)

_latest = {
    "cpu_percent": 0.0,
    "memory": {"total": 0, "used": 0},
    "swap": {"total": 0, "used": 0},
    "disk": {"total": 0, "used": 0},
    "net_totals": {"rx": 0, "tx": 0},
}


def _read_net_bytes() -> tuple[int, int]:
    with open("/proc/net/dev") as f:
        for line in f:
            if line.strip().startswith(f"{IFACE}:"):
                parts = line.split(":")[1].split()
                return int(parts[0]), int(parts[8])
    return 0, 0


def _read_cpu_jiffies() -> tuple[int, int]:
    with open("/proc/stat") as f:
        parts = f.readline().split()
    vals = list(map(int, parts[1:]))
    idle = vals[3] + vals[4]
    return idle, sum(vals)


def _memory() -> dict:
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            info[k.strip()] = int(v.strip().split()[0]) * 1024
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", 0)
    return {"total": total, "used": max(0, total - available)}


def _swap() -> dict:
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            info[k.strip()] = int(v.strip().split()[0]) * 1024
    total = info.get("SwapTotal", 0)
    free = info.get("SwapFree", 0)
    return {"total": total, "used": max(0, total - free)}


def _disk() -> dict:
    du = shutil.disk_usage("/")
    return {"total": du.total, "used": du.used}


def sample() -> None:
    """Call once per poller cycle. Builds history and refreshes the cached
    'latest' snapshot so /api/system never has to block on a live read."""
    global _last_cpu, _last_net
    now = time.time()

    idle, total = _read_cpu_jiffies()
    cpu_pct = 0.0
    if _last_cpu is not None:
        d_idle = idle - _last_cpu[0]
        d_total = total - _last_cpu[1]
        if d_total > 0:
            cpu_pct = round(max(0.0, 1 - d_idle / d_total) * 100, 1)
    _last_cpu = (idle, total)
    _cpu_history.append({"t": now, "v": cpu_pct})

    mem = _memory()
    mem_pct = round((mem["used"] / mem["total"]) * 100, 1) if mem["total"] else 0.0
    _mem_history.append({"t": now, "v": mem_pct})

    swap = _swap()
    swap_pct = round((swap["used"] / swap["total"]) * 100, 1) if swap["total"] else 0.0
    _swap_history.append({"t": now, "v": swap_pct})

    disk = _disk()
    disk_pct = round((disk["used"] / disk["total"]) * 100, 1) if disk["total"] else 0.0
    _disk_history.append({"t": now, "v": disk_pct})

    rx, tx = _read_net_bytes()
    if _last_net is not None:
        dt = now - _last_net[0]
        if dt > 0:
            rx_rate = max(0.0, (rx - _last_net[1]) / dt)
            tx_rate = max(0.0, (tx - _last_net[2]) / dt)
            _net_history.append({"t": now, "rx_bps": rx_rate, "tx_bps": tx_rate})
    _last_net = (now, rx, tx)

    _latest["cpu_percent"] = cpu_pct
    _latest["memory"] = mem
    _latest["swap"] = swap
    _latest["disk"] = disk
    _latest["net_totals"] = {"rx": rx, "tx": tx}


def record_connections(tcp_count: int, udp_count: int) -> None:
    _conn_history.append({"t": time.time(), "tcp": tcp_count, "udp": udp_count})


def uptime_seconds() -> float:
    with open("/proc/uptime") as f:
        return float(f.readline().split()[0])


def process_memory_bytes() -> int:
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    return 0


def snapshot() -> dict:
    return {
        "cpu_percent": _latest["cpu_percent"],
        "memory": _latest["memory"],
        "swap": _latest["swap"],
        "disk": _latest["disk"],
        "uptime_seconds": uptime_seconds(),
        "net_totals": _latest["net_totals"],
        "network_history": list(_net_history),
        "cpu_history": list(_cpu_history),
        "mem_history": list(_mem_history),
        "swap_history": list(_swap_history),
        "disk_history": list(_disk_history),
        "conn_history": list(_conn_history),
        "process_mem_bytes": process_memory_bytes(),
    }
