import asyncio
import logging
from collections import defaultdict

from . import db, endpoints as endpoints_mod, mgmt, netlimit, notify, settings, system

log = logging.getLogger("ovpn-dashboard.poller")

INTERVAL_SECONDS = 15

_endpoint_was_active: dict[str, bool] = {}


def reconcile() -> None:
    endpoints = db.get_all_endpoints()
    if not endpoints:
        return
    endpoints_by_name = {e["name"]: e for e in endpoints}

    sessions = mgmt.get_all_sessions(endpoints)

    def _protocol(endpoint_name: str) -> str:
        ep = endpoints_by_name.get(endpoint_name)
        return ep["protocol"] if ep else ""

    tcp_count = sum(1 for s in sessions if _protocol(s.endpoint) == "tcp")
    udp_count = sum(1 for s in sessions if _protocol(s.endpoint) == "udp")
    system.record_connections(tcp_count, udp_count)

    by_name: dict[str, list[mgmt.Session]] = defaultdict(list)
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
        delta = total if baseline is None else max(0, total - baseline)
        db.set_baseline(s.endpoint, s.client_id, s.name, total)
        if delta:
            db.ensure_client_row(s.name)
            db.add_used_bytes(s.name, delta)

    limits = db.get_all_limits()
    cfg = settings.load()

    def kill(session: mgmt.Session) -> None:
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
            notify.notify(cfg, "quota_exceeded", f'Client "{name}" hit its data quota and was disabled.')

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
            notify.notify(
                cfg, "max_connections",
                f'Client "{name}" exceeded its {max_conn}-connection limit; {len(excess)} extra session(s) killed.',
            )

    # endpoint up/down transitions
    for ep in endpoints:
        active = endpoints_mod.is_active(ep["name"])
        was = _endpoint_was_active.get(ep["name"])
        if was is not None and was and not active:
            notify.notify(cfg, "endpoint_down", f'Endpoint "{ep["name"]}" is no longer active.')
        elif was is not None and not was and active:
            notify.notify(cfg, "endpoint_up", f'Endpoint "{ep["name"]}" is back up.')
        _endpoint_was_active[ep["name"]] = active


async def run_forever() -> None:
    while True:
        await asyncio.sleep(INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(system.sample)
            await asyncio.to_thread(reconcile)
        except Exception:
            log.exception("poller cycle failed")
