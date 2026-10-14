import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field

from . import db, endpoints as endpoints_mod, mgmt, netlimit, system

log = logging.getLogger("ovpn-agent.poller")

INTERVAL_SECONDS = 15

_endpoint_was_active: dict[str, bool] = {}


@dataclass
class ReconcileResult:
    events: list[tuple[str, str]] = field(default_factory=list)
    tcp_count: int = 0
    udp_count: int = 0


def reconcile() -> ReconcileResult:
    """Runs one enforcement cycle against this box's own OpenVPN endpoints.
    events are (event, message) pairs for the caller to forward to the hub as
    notify events — the agent has no webhook/Telegram settings of its own,
    those live on the dashboard."""
    result = ReconcileResult()

    endpoints = db.get_all_endpoints()
    if not endpoints:
        return result
    endpoints_by_name = {e["name"]: e for e in endpoints}

    sessions = mgmt.get_all_sessions(endpoints)

    def _protocol(endpoint_name: str) -> str:
        ep = endpoints_by_name.get(endpoint_name)
        return ep["protocol"] if ep else ""

    result.tcp_count = sum(1 for s in sessions if _protocol(s.endpoint) == "tcp")
    result.udp_count = sum(1 for s in sessions if _protocol(s.endpoint) == "udp")

    by_name: dict[str, list] = defaultdict(list)
    for s in sessions:
        if s.client_id:
            by_name[s.name].append(s)

    # accumulate usage + prune stale baselines, per endpoint (client_id is only unique within one endpoint's mgmt instance)
    for ep in endpoints:
        active_ids = {s.client_id for s in sessions if s.endpoint == ep["name"] and s.client_id}
        db.prune_baselines(ep["name"], active_ids)

    for s in sessions:
        if not s.client_id:
            continue
        total = s.bytes_received + s.bytes_sent
        baseline = db.get_baseline(s.endpoint, s.client_id)
        # OpenVPN restarts client IDs from 0 after an endpoint restart, so a reconnecting
        # client can inherit a dead session's (larger) baseline. A counter below its
        # baseline can only be a new session: count all of it.
        delta = total if baseline is None or total < baseline else total - baseline
        db.set_baseline(s.endpoint, s.client_id, s.name, total)
        if delta:
            db.ensure_client_row(s.name)
            db.add_used_bytes(s.name, delta)

    limits = db.get_all_limits()

    def kill(session) -> None:
        ep = endpoints_by_name.get(session.endpoint)
        if not ep:
            return
        try:
            mgmt.kill_client_id(ep["mgmt_port"], session.client_id)
        except OSError:
            pass

    # enforce quota (aggregated across all endpoints)
    for name, sessions_for_name in by_name.items():
        row = limits.get(name)
        if not row:
            continue
        if row["quota_bytes"] and row["used_bytes"] >= row["quota_bytes"] and not row["disabled"]:
            log.info("client %s exceeded quota, killing + disabling on all endpoints", name)
            for s in sessions_for_name:
                kill(s)
            db.update_limits(name, disabled=1)
            client_ips = db.get_client_ips(name)
            for ep in endpoints:
                netlimit.write_ccd(ep["ccd_dir"], name, client_ips.get(ep["name"]), disabled=True)
            result.events.append(("quota_exceeded", f'Client "{name}" hit its data quota and was disabled.'))

    # enforce max_connections (aggregated across all endpoints): keep the oldest N, kill the rest
    for name, sessions_for_name in by_name.items():
        row = limits.get(name)
        max_conn = row["max_connections"] if row else 1
        if len(sessions_for_name) > max_conn:
            ordered = sorted(sessions_for_name, key=lambda s: s.connected_since_t)
            excess = ordered[max_conn:]
            for s in excess:
                log.info("client %s over max_connections (%d), killing session %s on %s",
                          name, max_conn, s.client_id, s.endpoint)
                kill(s)
            result.events.append((
                "max_connections",
                f'Client "{name}" exceeded its {max_conn}-connection limit; {len(excess)} extra session(s) killed.',
            ))

    # endpoint up/down transitions
    for ep in endpoints:
        active = endpoints_mod.is_active(ep["name"])
        if active:
            _restore_bandwidth_caps(ep, limits)
        was = _endpoint_was_active.get(ep["name"])
        if was is not None and was and not active:
            result.events.append(("endpoint_down", f'Endpoint "{ep["name"]}" is no longer active.'))
        elif was is not None and not was and active:
            result.events.append(("endpoint_up", f'Endpoint "{ep["name"]}" is back up.'))
        _endpoint_was_active[ep["name"]] = active

    return result


def _restore_bandwidth_caps(ep, limits) -> None:
    """tc state lives on the tun device, which OpenVPN recreates on every restart,
    stop/start, crash or reboot, silently dropping every cap on that endpoint. A missing
    root qdisc is the cheap tell that this happened."""
    if netlimit.has_tc_root(ep["tun_dev"]):
        return
    ip_to_kbps = {}
    for name, row in limits.items():
        if not row["bandwidth_kbps"]:
            continue
        ip = db.get_client_ips(name).get(ep["name"])
        if ip:
            ip_to_kbps[ip] = row["bandwidth_kbps"]
    if ip_to_kbps:
        log.info("re-applying %d bandwidth cap(s) on %s", len(ip_to_kbps), ep["tun_dev"])
        netlimit.reapply_endpoint(ep["tun_dev"], ip_to_kbps)


async def run_forever() -> None:
    """Transitional: runs the enforcement loop locally, same cadence as
    before. Events from reconcile() are logged, not delivered anywhere yet —
    once the hub/WS link exists (transport step), this loop moves into
    agent/main.py and forwards events there instead of dropping them."""
    while True:
        await asyncio.sleep(INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(system.sample)
            result = await asyncio.to_thread(reconcile)
            for event, message in result.events:
                log.info("event (no hub connected yet, dropped): %s: %s", event, message)
            system.record_connections(result.tcp_count, result.udp_count)
        except Exception:
            log.exception("poller cycle failed")
