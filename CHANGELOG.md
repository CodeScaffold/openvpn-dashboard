# Changelog

## 2.0.0 — Multi-server

The dashboard now manages **any number of OpenVPN servers from one panel**, not just the
box it runs on. This is a full architectural rebuild, not an incremental update — see
below for what that means if you're upgrading from 1.x.

### Added

- **Multi-server support.** Add any Linux box you control as a VPN exit by installing a
  small agent on it and pointing it at your dashboard. No extra domains needed — agents
  dial *out* to the dashboard over one WebSocket, so they work from behind NAT/CGNAT with
  no inbound port required.
- **Independent PKI per server.** Each server keeps its own CA. A certificate issued on
  one server is cryptographically meaningless on any other, so there's no way — accidental
  or otherwise — for a client to end up on an exit point nobody assigned them to.
- **Admin-pinned static IPs.** Assign a client a fixed exit IP on a given endpoint from the
  admin UI. It's the admin's call, not the client's — nothing in the product lets a user
  pick or change their own IP, and a pin now survives unrelated limit/bandwidth edits
  (previously, removing a bandwidth cap silently stripped a client's IP — fixed as part of
  making static IPs a first-class feature).
- **Client teams.** Optionally group clients by team for filtering.
- **Servers page.** Add/remove servers, see online status and agent version, one-time
  enrollment tokens.
- **Server switcher** in the header, with per-server dashboards, endpoints, and clients.
- **Docker install for both hub and agent**, plus a `docker-compose.test.yml` reference
  setup showing a full hub + 2-agent deployment.
- **Live GitHub Pages demo** with two demo servers, showing the new multi-server UI end to
  end without installing anything.

### Changed

- **Split into a hub and an agent.** The hub (`app/`) is now a thin broker: dashboard UI,
  server registry, settings, and a live-mirrored display copy of each server's state. It no
  longer touches OpenVPN, `easy-rsa`, or any Linux-specific tooling directly, and no longer
  needs root, `systemctl`, or host networking. All of that moved to the agent (`agent/`),
  which runs on each VPN box and does what the old single-box dashboard used to do itself.
- **Telemetry is pushed, not polled.** Each agent runs the metrics/session-reconciliation
  loop locally and pushes results to the hub, instead of the hub opening blocking sockets
  to the VPN process itself.
- **Agents are autonomous.** Quotas and connection limits are enforced locally by the
  agent and keep working even if the hub is unreachable.
- **Backups are per-server.** Each agent bundles its own CA, endpoint configs, and client
  database; the hub's settings back up separately.

### Upgrading from 1.x

There's no in-place migration — this is a different process topology. The straightforward
path: stand up the new hub, install an agent on your existing VPN box, and re-add it as
your first server. Your existing OpenVPN clients keep connecting throughout; only the
admin panel changes. See the README's Docker or manual install sections.

### Security

- Endpoint control on the agent now runs through a narrow, name-validating wrapper script
  instead of a wildcard `sudo` rule — needed for `sudo-rs`-based systems (default on newer
  Ubuntu), which reject wildcards embedded in a single command argument, and a tighter
  design regardless of which `sudo` implementation is in use.

## 1.0.0 — Initial release

Single-box FastAPI dashboard for one OpenVPN server: multiple endpoints (listeners) on
that one box, client issue/revoke/renew, per-client quotas and bandwidth caps, live
dashboard metrics, backup/restore, and notification hooks.
