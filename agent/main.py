"""Agent entrypoint: --selftest exercises the op dispatch table (agent/ops.py)
directly with no hub connection, for validating a fresh install and for CI.
Otherwise this dials the hub over a WebSocket, authenticates with an
enrollment token, and serves ops + pushes telemetry until killed.
"""
import argparse
import asyncio
import dataclasses
import logging
import os
import random
import shutil
import socket
import sys
import tempfile
from pathlib import Path

import websockets

from common.wire import PROTOCOL_VERSION, Envelope, HelloData, event, response
from . import backup as backup_mod, db, endpoints as endpoints_mod, ops, pki, poller, system

log = logging.getLogger("ovpn-agent")

AGENT_VERSION = "0.1.0-dev"

RECONNECT_MIN_SECONDS = 1
RECONNECT_MAX_SECONDS = 60


def _redirect_paths(sandbox: Path) -> None:
    """Point every module-level path constant at a throwaway sandbox so
    --selftest never touches the real /etc/openvpn on the box it runs on."""
    db.DB_PATH = sandbox / "limits.db"
    db._local = __import__("threading").local()

    pki.EASYRSA_DIR = sandbox / "easy-rsa"
    pki.SERVER_CRL = sandbox / "server" / "crl.pem"
    (pki.EASYRSA_DIR / "pki").mkdir(parents=True, exist_ok=True)
    (sandbox / "server").mkdir(parents=True, exist_ok=True)

    endpoints_mod.SERVER_DIR = sandbox / "server"
    ops.SERVER_DIR = sandbox / "server"

    backup_mod.EASYRSA_PKI = pki.EASYRSA_DIR / "pki"
    backup_mod.CCD_ROOT = sandbox / "ccd"
    backup_mod.SERVER_DIR = sandbox / "server"
    backup_mod.DB_FILE = db.DB_PATH
    backup_mod.MEMBER_ROOTS = {
        "pki": backup_mod.EASYRSA_PKI,
        "ccd": backup_mod.CCD_ROOT,
        "server-conf": backup_mod.SERVER_DIR,
        "db": backup_mod.DB_FILE.parent,
    }


def _check(label: str, fn) -> bool:
    try:
        fn()
        print(f"  [ok]   {label}")
        return True
    except Exception as e:  # noqa: BLE001 - selftest wants to report, not raise
        print(f"  [FAIL] {label}: {e}")
        return False


def run_selftest() -> int:
    print(f"ovpn-agent {AGENT_VERSION} selftest")
    sandbox = Path(tempfile.mkdtemp(prefix="ovpn-agent-selftest-"))
    _redirect_paths(sandbox)
    ok = True

    print("\nDatabase + migrations:")
    ok &= _check("db.get_conn() bootstraps schema", lambda: db.get_conn())
    ok &= _check("schema_version lands on the latest migration", _check_schema_version)

    print("\nOp dispatch (no privileged tools required):")
    ok &= _check("client.list on empty PKI returns []", lambda: _expect(ops.dispatch("client.list", {}), {"clients": []}))
    ok &= _check("endpoint.list on empty db returns []", lambda: _expect(ops.dispatch("endpoint.list", {}), {"endpoints": []}))
    ok &= _check("unknown op raises OpError", lambda: _expect_raises(lambda: ops.dispatch("nonsense.op", {}), ops.OpError))
    ok &= _check(
        "client.create rejects a reserved name",
        lambda: _expect_raises(
            lambda: ops.dispatch("client.create", {"name": "server", "validity_days": 365}), ops.OpError
        ),
    )

    print("\nPrivileged-tool availability on this box (informational, not fail):")
    for tool in ("easyrsa", "openvpn", "systemctl", "tc", "ufw", "ss", "journalctl", "sudo"):
        found = shutil.which(tool) is not None
        print(f"  [{'ok' if found else '--'}]   {tool} {'found' if found else 'not found — full lifecycle test skipped'}")

    have_easyrsa = shutil.which("easyrsa") is not None
    if have_easyrsa:
        print("\nFull endpoint+client lifecycle (requires root + systemd + easy-rsa):")
        ok &= _check("build a real PKI and issue/revoke a throwaway client", lambda: _full_lifecycle(sandbox))
    else:
        print("\nSkipping full lifecycle test — install on the target Linux box to exercise it.")

    shutil.rmtree(sandbox, ignore_errors=True)
    print("\nselftest: PASS" if ok else "\nselftest: FAIL")
    return 0 if ok else 1


def _check_schema_version() -> None:
    row = db.get_conn().execute("SELECT version FROM schema_version").fetchone()
    latest = db.MIGRATIONS[-1][0]
    if row[0] != latest:
        raise AssertionError(f"expected version {latest}, got {row[0]}")


def _expect(actual, expected) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _expect_raises(fn, exc_type) -> None:
    try:
        fn()
    except exc_type:
        return
    raise AssertionError(f"expected {exc_type.__name__} to be raised")


def _full_lifecycle(sandbox: Path) -> None:
    # Left as a real end-to-end check for the target box: build a throwaway
    # easy-rsa PKI, issue a client, download its .ovpn, revoke it. Requires
    # privileges this selftest deliberately does not assume it has when run
    # outside its target environment (see the tool-availability scan above).
    raise NotImplementedError("full lifecycle selftest runs only on an installed agent box (step 7)")


# ---------------- WebSocket client ----------------

async def _dispatch_loop(ws) -> None:
    """Serves ops requested by the hub until the connection drops."""
    async for raw in ws:
        try:
            envelope = Envelope.from_json(raw)
        except Exception:
            log.warning("malformed envelope from hub, dropping")
            continue
        if envelope.kind != "req":
            continue
        try:
            data = ops.dispatch(envelope.op, envelope.data)
            await ws.send(response(envelope, data=data).to_json())
        except ops.OpError as e:
            await ws.send(response(envelope, error=str(e), error_code=e.code).to_json())
        except Exception as e:  # noqa: BLE001 - must not crash the connection on a bad op
            log.exception("op %s crashed", envelope.op)
            await ws.send(response(envelope, error=f"internal error: {e}", error_code="internal").to_json())


async def _telemetry_loop(ws, stop_event: asyncio.Event) -> None:
    """Runs the same enforcement cycle poller.reconcile() always ran, and
    pushes (event, snapshot) pairs to the hub instead of calling notify/
    settings directly — the agent has neither."""
    while not stop_event.is_set():
        await asyncio.sleep(poller.INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(endpoints_mod.ensure_bind_ip)
            await asyncio.to_thread(system.sample)
            result = await asyncio.to_thread(poller.reconcile)
            for event_name, message in result.events:
                await ws.send(event("notify", {"event": event_name, "message": message}).to_json())

            snapshot = system.snapshot()
            latest_net = snapshot["network_history"][-1] if snapshot["network_history"] else {}
            snapshot["net_rx_bps"] = latest_net.get("rx_bps", 0.0)
            snapshot["net_tx_bps"] = latest_net.get("tx_bps", 0.0)
            snapshot["tcp_count"] = result.tcp_count
            snapshot["udp_count"] = result.udp_count
            snapshot["agent_version"] = AGENT_VERSION
            # the hub's display mirror (server_endpoints/server_clients) is fed from here —
            # cheap on a real box (a handful of endpoints/clients), no separate poll needed.
            snapshot["endpoints"] = ops.op_endpoint_list({})["endpoints"]
            snapshot["clients"] = ops.op_client_list({})["clients"]
            await ws.send(event("telemetry", snapshot).to_json())
        except websockets.ConnectionClosed:
            return
        except Exception:
            log.exception("telemetry cycle failed")


async def run_once(hub_url: str, token: str) -> None:
    async with websockets.connect(hub_url) as ws:
        hello = HelloData(
            token=token, agent_version=AGENT_VERSION, hostname=socket.gethostname(),
        )
        await ws.send(event("hello", dataclasses.asdict(hello)).to_json())
        log.info("connected to hub at %s", hub_url)

        stop_event = asyncio.Event()
        telemetry_task = asyncio.create_task(_telemetry_loop(ws, stop_event))
        try:
            await _dispatch_loop(ws)
        finally:
            stop_event.set()
            telemetry_task.cancel()


async def run_forever(hub_url: str, token: str) -> None:
    backoff = RECONNECT_MIN_SECONDS
    while True:
        try:
            await run_once(hub_url, token)
            backoff = RECONNECT_MIN_SECONDS  # clean disconnect (hub restart, etc.) — don't punish the next attempt
        except (OSError, websockets.InvalidStatus, websockets.ConnectionClosedError) as e:
            log.warning("hub connection lost (%s), retrying in %.1fs", e, backoff)
        except Exception:
            log.exception("agent loop crashed, retrying in %.1fs", backoff)
        await asyncio.sleep(backoff + random.uniform(0, backoff * 0.1))
        backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ovpn-agent")
    parser.add_argument("--selftest", action="store_true", help="Run local op checks and exit, no hub connection.")
    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest()

    logging.basicConfig(level=logging.INFO)
    hub_url = os.environ["HUB_URL"]
    token = os.environ["AGENT_TOKEN"]
    try:
        endpoints_mod.reconcile_endpoints()
    except Exception:
        log.exception("Endpoint reconciliation failed at startup -- continuing anyway")
    try:
        endpoints_mod.reconcile_nat()
    except Exception:
        log.exception("NAT reconciliation failed at startup -- continuing anyway")
    asyncio.run(run_forever(hub_url, token))
    return 0


if __name__ == "__main__":
    sys.exit(main())
