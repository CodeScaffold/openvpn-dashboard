# ovpn-dashboard

A self-hosted admin panel for OpenVPN, built to manage **any number of servers from one dashboard**. FastAPI backend, no-build-step vanilla HTML/CSS/JS frontend. Built for people who want a real UI on top of OpenVPN — creating clients, setting limits, watching traffic, running exit points in more than one place — instead of hand-editing configs and juggling `easy-rsa` from memory.

**[Try the live demo →](https://codescaffold.github.io/openvpn-dashboard/demo/)** — the real frontend, running against fake (but realistic, live-updating) data, including two demo servers. No install, no login, nothing to set up. Click around: switch servers, create a client, hover the charts, poke through Settings.

**New in 2.0:** multi-server support — see [CHANGELOG.md](CHANGELOG.md) for what changed from 1.x.

## What it does

- **Servers** — add any Linux box you control as a VPN exit by installing a small agent on it and pointing it at your dashboard. Each server has its own independent CA — a certificate issued on one server is cryptographically meaningless on any other, so there's no way (accidental or otherwise) for a client to end up on an exit point nobody assigned them to.
- **Endpoints** — per server, independent OpenVPN listeners, each with its own port/protocol/cipher, each its own systemd unit. Run one on `1194/udp` for normal use and another on `443/tcp` so it's indistinguishable from ordinary HTTPS on a restrictive network. Every client on that server can connect through any of its endpoints.
- **Clients** — issue/revoke/renew certificates, per-client max simultaneous connections, data quota, and bandwidth cap, enforced live via `tc` and OpenVPN's management interface. Certificate validity is a picker (1 month / 3 months / 1 year / ... / a specific date), not a raw day count. Optionally group clients by team.
- **Admin-pinned static IPs** — assign a client a fixed exit IP on a given endpoint from the admin UI. It's the admin's call, not the client's: nothing in the product lets a user pick or change their own IP, and a pin survives unrelated limit/bandwidth edits. This is what makes IP-based access control in some other system (e.g. "only requests from this IP get a token") actually safe to build on.
- **Dashboard** — live CPU/RAM/swap/disk per server, a real interactive throughput chart (hover for a crosshair + exact values at that point in time), connection counts by transport, uptime, and a health banner when something's elevated.
- **Settings** — idle session timeout, login lockout tuning, sign out every other session, webhook/Telegram notifications (quota hit, connection-limit kill, endpoint down, manual restart), new-client defaults, one-click backup/restore, metrics retention window.
- **Backup & restore** — per server, one button bundles that server's CA, every endpoint config, and its client database into a `.tar.gz`. The dashboard's own settings back up separately. Restore auto-snapshots the current state first.

## Architecture

Two kinds of process, talking over one WebSocket:

- **The hub** (`app/`) is the dashboard you log into. It's a small FastAPI app holding the server registry, your settings, and a live-mirrored copy of each server's endpoints/clients/metrics for display. It never touches OpenVPN, `easy-rsa`, or any Linux-specific tooling directly — it doesn't even need to run on Linux, or on the same continent as your VPN boxes.
- **An agent** (`agent/`) runs on each VPN box. It owns that box's PKI, talks to `easy-rsa` for certs, to OpenVPN's management sockets for live sessions, and to `tc`/`ufw`/`systemctl` via a handful of tightly-scoped `sudo` grants — everything the old single-box version did, just relocated. It dials **out** to the hub over a WebSocket (`wss://<hub>/agent/ws`) and stays connected; the hub never needs to reach back in, so agents work from behind NAT/CGNAT with nothing but one outbound connection.

Enrollment is a one-time token: create a server in the hub's UI, get a token and an install command, run it on the box. The token is shown once and only its hash is ever stored. Each agent's PKI is entirely its own — there is no shared CA across servers, by design (see the static-IP note above for why that matters).

No message queue, no separate frontend build, no Redis, no external database — SQLite and flat files on both sides, on purpose.

## Docker (fastest install)

### 1. The hub

The hub is a normal, unprivileged container — it holds no VPN state, so it needs no special capabilities or host networking.

```bash
git clone https://github.com/CodeScaffold/openvpn-dashboard
cd openvpn-dashboard
docker compose up -d --build
docker compose logs | grep "Admin password"   # shown once, on first boot only
```

Visit `http://YOUR_HOST:8080`, log in with that password, and change it from **Settings → Account**. Put a real reverse proxy + TLS in front of it for anything beyond local testing (see [Reverse proxy + TLS](#reverse-proxy--tls) below) — the hub's own port never needs to be exposed directly on the internet.

Data (settings, the server registry, the display mirror) lives in the `hub-config` named volume — `docker compose down` is safe, `docker compose down -v` deletes everything.

**Updating:** `git pull && docker compose up -d --build`.

### 2. Add a server

From the dashboard: **Servers → Add server**. Give it a name and the address its clients should dial (a subdomain you already own, or a bare IP — no new domain purchase needed). You'll get a one-time install command with an embedded token.

### 3. Install the agent on that box

Either run the one-liner the dashboard just showed you:

```bash
curl -fsSL https://raw.githubusercontent.com/CodeScaffold/openvpn-dashboard/main/agent/install.sh \
  | sudo HUB_URL=wss://panel.example.com/agent/ws AGENT_TOKEN=<token> bash
```

...or run it in Docker instead, on that same box:

```bash
git clone https://github.com/CodeScaffold/openvpn-dashboard
cd openvpn-dashboard
docker build -f docker/agent.Dockerfile -t ovpn-agent .
docker run -d --name ovpn-agent --restart unless-stopped \
  --privileged --cgroupns=host \
  -v ovpn-openvpn:/etc/openvpn -v ovpn-agent-config:/etc/ovpn-dashboard -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  --network host \
  -e HUB_URL=wss://panel.example.com/agent/ws -e AGENT_TOKEN=<token> \
  ovpn-agent
```

The agent shows online in the dashboard within a few seconds. Repeat step 2 and 3 for every additional server — each is fully independent.

**Why the agent needs `--privileged` and host networking, but the hub doesn't:** OpenVPN needs to configure real NAT/routing for tunneled traffic, which bridge networking can't do transparently — this is standard for any containerized VPN server. The hub does none of that, so it stays a completely ordinary container.

### Multiple exit IPs on one box

Add-on IPs (e.g. Hetzner Floating IPs) can each back their own fully independent server —
own PKI, own agent identity, own entry on the Servers page — instead of one server per
physical box. Repeat steps 2–3 above once per IP, and on step 3 add one more variable:

```bash
-e BIND_IP=<that IP>
```

(`sudo BIND_IP=<that IP> HUB_URL=... AGENT_TOKEN=... bash` for the non-Docker installer.)
That pins the agent's OpenVPN listeners and its clients' outbound NAT to exactly that
address — not the box's default one — so several agents can share a host, each answering
only on its own IP, even on the same port.

**Add the IP to the box's own persistent network config first — this step is required,
not optional.** A Hetzner Floating IP (or similar) assigned in a Cloud Console is *routed*
to the box, but nothing puts it on the network interface until you do. Running `ip addr
add` by hand (or relying only on the agent's own bookkeeping) is not enough either: on a
box that manages its interface via DHCP (netplan/systemd-networkd, the default on Ubuntu),
the next lease renewal reconciles the interface back to whatever's in netplan's own
config and silently drops anything added outside of it — possibly hours after the agent
started, with no warning beyond OpenVPN failing to bind the next time it (re)starts.

Add it as a static address on the same interface, in its own file so a `cloud-init` rerun
doesn't wipe it:

```yaml
# /etc/netplan/60-ovpn-floating-ips.yaml
network:
  version: 2
  ethernets:
    eth0:                      # match your actual interface name
      match:
        macaddress: "..."      # copy from your existing netplan config
      set-name: "eth0"
      addresses:
      - 203.0.113.5/32         # one line per floating IP on this box
```

```bash
netplan generate && netplan apply
```

*Not on Ubuntu/netplan?* Add the IP as a static address on the interface using whatever
your distro's network manager provides (`systemd-networkd` `.network` file directly,
`NetworkManager` connection profile, etc.) — the goal is the same either way: the address
has to be part of the box's own declared network state, not just imperatively assigned
once. The agent's `ovpn-bind-ip-ctl` also re-checks and re-adds the address on its own
periodically as a fallback, but that's a safety net for drift, not a substitute for this.

Not interested in Docker? Same install, done by hand — expand below.

<details>
<summary><strong>Manual install (no Docker) — click to expand</strong></summary>

## Requirements

- **The hub**: any box with Python 3.10+. Doesn't need to be Linux, doesn't need root, doesn't need to be anywhere near your VPN servers.
- **Each agent**: a Linux box you have root on. Built and tested on **Ubuntu 24.04**; likely fine on 22.04 and other systemd distros with path tweaks. A public IP.

Everything below is copy-pasteable. Replace `panel.example.com` and `vpn.example.com` with your own.

---

## Part A — the hub

### A1. Get the code and install dependencies

```bash
git clone https://github.com/CodeScaffold/openvpn-dashboard /opt/ovpn-dashboard
cd /opt/ovpn-dashboard
python3 -m venv venv
venv/bin/pip install -r requirements.txt
useradd --system --home /opt/ovpn-dashboard --shell /usr/sbin/nologin ovpndash
chown -R ovpndash:ovpndash /opt/ovpn-dashboard
```

### A2. Secrets and config

```bash
mkdir -p /etc/ovpn-dashboard
python3 - <<'EOF'
import hashlib, secrets

password = "CHANGE_ME_NOW"   # you'll change this from Settings → Account after first login anyway
salt = secrets.token_bytes(16)
digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)

print(f"SESSION_SECRET={secrets.token_hex(32)}")
print(f"ADMIN_PASSWORD_SALT={salt.hex()}")
print(f"ADMIN_PASSWORD_HASH={digest.hex()}")
EOF
```

Paste that output into `/etc/ovpn-dashboard/config.env`:

```bash
chmod 600 /etc/ovpn-dashboard/config.env
chown ovpndash:ovpndash /etc/ovpn-dashboard/config.env
```

That's the entire config — no `VPN_ENDPOINT` here anymore; each server's own dial-in address is set when you add it in the UI.

### A3. systemd service

```bash
cat > /etc/systemd/system/ovpn-dashboard.service <<'EOF'
[Unit]
Description=OpenVPN admin dashboard (hub)
After=network.target

[Service]
User=ovpndash
Group=ovpndash
WorkingDirectory=/opt/ovpn-dashboard
EnvironmentFile=/etc/ovpn-dashboard/config.env
ExecStart=/opt/ovpn-dashboard/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080
Restart=on-failure
RestartSec=3
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/etc/ovpn-dashboard

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now ovpn-dashboard
systemctl status ovpn-dashboard   # should say "active (running)"
```

Notice what's *not* here compared to before: no `AmbientCapabilities`, no `/etc/openvpn/*` in `ReadWritePaths`. The hub genuinely doesn't touch any of that anymore.

### Reverse proxy + TLS

```bash
apt install -y nginx certbot python3-certbot-nginx

cat > /etc/nginx/sites-available/panel.example.com.conf <<'EOF'
server {
    listen 80;
    server_name panel.example.com;
    location / { return 301 https://$host$request_uri; }
}

server {
    listen 443 ssl http2;
    server_name panel.example.com;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # the agent WebSocket needs the Upgrade headers too — same location block covers it
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
EOF

ln -s /etc/nginx/sites-available/panel.example.com.conf /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
certbot --nginx -d panel.example.com   # fills in ssl_certificate lines and reloads nginx for you
```

If you'd rather not run a public panel domain at all, skip this and SSH-tunnel to `127.0.0.1:8080` (`ssh -L 8080:127.0.0.1:8080 root@your-server`) — the hub never needs to be internet-facing for you to use it. Agents do need to reach it though, so this only works if every agent box can also reach `127.0.0.1:8080` through the same tunnel (or you're only ever running agents on the hub box itself).

### Updating the hub

```bash
cd /opt/ovpn-dashboard
git pull
venv/bin/pip install -r requirements.txt   # only needed if requirements.txt changed
systemctl restart ovpn-dashboard
```

---

## Part B — each agent

Once the hub is up, add a server from its UI (**Servers → Add server**) to get an install token, then either run `agent/install.sh` (does everything below in one shot — see [the Docker section above](#3-install-the-agent-on-that-box) for the exact command) or follow these steps by hand to understand what it's doing.

### B1. Base packages

```bash
apt update
apt install -y openvpn easy-rsa python3-venv python3-pip git ufw
```

### B2. Build this server's own PKI

```bash
make-cadir /etc/openvpn/easy-rsa
cd /etc/openvpn/easy-rsa
```

Edit `vars` (create it if it doesn't exist) — this is the one place cert defaults live:

```bash
cat > vars <<'EOF'
set_var EASYRSA_ALGO ec
set_var EASYRSA_CURVE prime256v1
set_var EASYRSA_CA_EXPIRE 3650
set_var EASYRSA_CERT_EXPIRE 3650
set_var EASYRSA_BATCH 1
EOF
```

Now build it:

```bash
./easyrsa init-pki
./easyrsa build-ca nopass          # you'll be prompted for a CA Common Name — anything is fine
./easyrsa gen-req server nopass
./easyrsa sign-req server server   # type "yes" to confirm
openvpn --genkey secret pki/tls-crypt.key   # tls-crypt isn't an easy-rsa concept — it's OpenVPN's own static key
./easyrsa gen-crl
```

Copy the server identity files where OpenVPN expects them:

```bash
mkdir -p /etc/openvpn/server /etc/openvpn/ccd /etc/openvpn/clients
cp pki/ca.crt pki/issued/server.crt pki/private/server.key pki/tls-crypt.key pki/crl.pem /etc/openvpn/server/
chmod 600 /etc/openvpn/server/server.key
```

This CA is entirely local to this box — nothing here is shared with any other server you add. There's no endpoint or `server.conf` yet either; unlike the old single-box version, that's not part of install anymore. You'll create your first endpoint from the dashboard's Endpoints page once the agent connects (below), exactly the same action as creating a second or third one.

### B3. Routing: IP forwarding, NAT, firewall

```bash
# persistent IP forwarding
echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/99-openvpn.conf
sysctl --system

# NAT: VPN clients need to masquerade as your real interface.
# Check your public interface name first (often eth0, sometimes ens3/enp1s0):
ip route get 1.1.1.1
```

Add this inside the existing `*nat` / `:POSTROUTING` block of `/etc/ufw/before.rules` (create the block if ufw hasn't added one yet — it goes above the `*filter` line). Note the `/16`, not `/24`: this server can host several endpoints, each on its own `10.9.<n>.0/24` slice, so the rule needs to cover the whole range up front.

```
*nat
:POSTROUTING ACCEPT [0:0]
-A POSTROUTING -s 10.9.0.0/16 -o eth0 -j MASQUERADE
COMMIT
```

Then allow forwarding between any tunnel interface and your public one, and open SSH:

```bash
ufw route allow in on tun+ out on eth0
ufw route allow in on eth0 out on tun+
ufw allow 22/tcp     # don't lock yourself out
ufw enable
```

Swap `eth0` for whatever `ip route`/`ip addr` actually show you — getting this interface name wrong is, by a wide margin, the most common reason "the VPN connects but no internet" happens. `tun+` matches every tunnel device this box will ever create (`tun0`, `tun1`, ...), so you only set this up once regardless of how many endpoints you add later.

### B4. The agent's own system user

The agent runs as an unprivileged user with a narrow, explicit set of `sudo` grants — never as root.

```bash
useradd --system --home /opt/ovpn-dashboard --shell /usr/sbin/nologin ovpndash
usermod -aG systemd-journal ovpndash   # lets it read logs for the viewer, nothing else

chown -R ovpndash:ovpndash /etc/openvpn/easy-rsa /etc/openvpn/ccd /etc/openvpn/clients
chown ovpndash:ovpndash /etc/openvpn/server
```

The `systemctl` grants need a small wrapper rather than a direct wildcard rule — some `sudo` implementations (notably `sudo-rs`, now the default on newer Ubuntu) reject a wildcard embedded inside a single argument like `openvpn-server@*`, even though a bare `*` as its own argument (like the `ufw` grants below) is still fine. The wrapper validates the endpoint name itself instead of relying on sudo's pattern matching, which is arguably the better design anyway:

```bash
cat > /usr/local/bin/ovpn-endpoint-ctl <<'EOF'
#!/bin/bash
set -euo pipefail
action="${1:-}"
name="${2:-}"
if ! [[ "$name" =~ ^[a-z0-9-]{1,24}$ ]]; then
  echo "invalid endpoint name" >&2
  exit 1
fi
case "$action" in
  restart) exec systemctl restart "openvpn-server@${name}" ;;
  enable-now) exec systemctl enable --now "openvpn-server@${name}" ;;
  disable-now) exec systemctl disable --now "openvpn-server@${name}" ;;
  disable) exec systemctl disable "openvpn-server@${name}" ;;
  *) echo "unknown action: $action" >&2; exit 1 ;;
esac
EOF
chmod 755 /usr/local/bin/ovpn-endpoint-ctl
```

Grant exactly these, and nothing more, via `visudo -f /etc/sudoers.d/ovpndash`:

```
ovpndash ALL=(root) NOPASSWD: /usr/local/bin/ovpn-endpoint-ctl
ovpndash ALL=(root) NOPASSWD: /usr/sbin/ufw allow *
ovpndash ALL=(root) NOPASSWD: /usr/sbin/ufw delete allow *
```

That's the entire trusted-computing surface: it can start/stop/restart OpenVPN endpoints it created (any endpoint name matching the wrapper's validation — every endpoint is equally manageable from the UI, not just a distinguished first one) and open/close firewall ports for them. It cannot restart itself, touch other services, or run arbitrary commands as root.

### B5. Get the code

```bash
git clone https://github.com/CodeScaffold/openvpn-dashboard /opt/ovpn-dashboard
cd /opt/ovpn-dashboard
python3 -m venv venv
venv/bin/pip install -r requirements.txt
chown -R ovpndash:ovpndash /opt/ovpn-dashboard
```

### B6. Config

```bash
mkdir -p /etc/ovpn-dashboard
cat > /etc/ovpn-dashboard/agent.env <<EOF
HUB_URL=wss://panel.example.com/agent/ws
AGENT_TOKEN=paste-the-one-time-token-from-Add-Server-here
EOF
chmod 600 /etc/ovpn-dashboard/agent.env
chown ovpndash:ovpndash /etc/ovpn-dashboard/agent.env
```

### B7. systemd service

```bash
cat > /etc/systemd/system/ovpn-agent.service <<'EOF'
[Unit]
Description=ovpn-dashboard agent
After=network-online.target
Wants=network-online.target

[Service]
User=ovpndash
Group=ovpndash
WorkingDirectory=/opt/ovpn-dashboard
EnvironmentFile=/etc/ovpn-dashboard/agent.env
ExecStart=/opt/ovpn-dashboard/venv/bin/python3 -m agent.main
Restart=on-failure
RestartSec=3
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/etc/openvpn/easy-rsa /etc/openvpn/clients /etc/openvpn/ccd /etc/openvpn/server /etc/ovpn-dashboard /etc/ufw /run

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now ovpn-agent
systemctl status ovpn-agent   # should say "active (running)"
```

If it doesn't come up, `journalctl -u ovpn-agent -n 50 --no-pager` will tell you why. Once it's running, the server shows online in the dashboard within a few seconds — go create your first endpoint.

### Updating an agent

```bash
cd /opt/ovpn-dashboard
git pull
venv/bin/pip install -r requirements.txt   # only needed if requirements.txt changed
systemctl restart ovpn-agent
```

Nothing here touches `/etc/openvpn/*` — application updates and VPN operation are fully decoupled, same as before.

## Using it

1. Log in, go to **Servers → Add server**, follow the install command it gives you.
2. Once a server shows online: **Endpoints → Create endpoint** — pick a port/protocol/cipher. `443/tcp` is a good second endpoint for networks that block anything that isn't ordinary HTTPS.
3. **Clients → Create client** — pick a name, a connection cap, optionally a quota/bandwidth cap, a team, and a validity period. It signs the cert on that server and offers the `.ovpn` file straight away.
4. Import that file into any OpenVPN client (Tunnelblick, OpenVPN Connect, NetworkManager, the official Android/iOS apps — anything that reads a standard `.ovpn`).
5. Need a client pinned to a specific exit IP? Open their **Limits** dialog — the Static IP section assigns and pins it; that assignment survives any later change to their bandwidth cap or quota.
6. Optional: **Settings → Notifications** for a webhook or Telegram bot ping when a client hits its quota, gets killed for too many connections, or an endpoint goes down.
7. Optional: per-server **Settings → Backup**, or from a server's card on the Servers page — download a `.tar.gz` now and put it somewhere durable. It contains that server's CA private key; treat it like a root credential, not a config file.

</details>

## Troubleshooting

<details>
<summary>Click to expand</summary>

**Client connects but has no internet.** Almost always the NAT masquerade rule pointing at the wrong interface, or `net.ipv4.ip_forward` not actually being `1` (`sysctl net.ipv4.ip_forward` to check — the sysctl.d file only takes effect after `sysctl --system` or a reboot).

**`ufw: unable to change to root gid`** after adding the agent's sudoers rules. You added `CapabilityBoundingSet=` to the systemd unit. Remove it — the bounding set also caps what `sudo` can escalate *to*, and `ufw` internally needs more than `CAP_NET_ADMIN`/`CAP_NET_RAW`. Ambient capabilities alone are the right amount of restriction here.

**`visudo: invalid sudoers file` / `wildcards are not allowed in command arguments`** when adding B4's sudoers rules. Your `sudo` is `sudo-rs` (default on Ubuntu 26.04+) or another implementation that rejects wildcards embedded inside a single argument — that's exactly what B4's `ovpn-endpoint-ctl` wrapper is for; make sure you created it before writing the sudoers file, and that the sudoers file points at the wrapper, not a raw `openvpn-server@*` pattern.

**`Failed to set up mount namespacing`, service won't start.** A path in `ReadWritePaths=` doesn't exist yet. Every directory listed there must exist *before* the service starts; `systemd` won't create them for you.

**A new endpoint's `client-config-dir` clients never get their static IP.** Endpoints created through the UI get their own `ccd` directory nested under `/etc/openvpn/ccd/<name>/` specifically because `ReadWritePaths=/etc/openvpn/ccd` only covers that path and its children — a sibling directory outside it would be read-only to the service no matter what you `chown`.

**Logs are empty in the dashboard's log viewer.** `ovpndash` needs to be in the `systemd-journal` group. Group membership from `usermod -aG` doesn't apply retroactively to a running process, so restart the `ovpn-agent` service (not just re-login) after adding it.

**Agent shows offline and never connects.** Check `journalctl -u ovpn-agent -n 50 --no-pager` on the agent box. The two usual causes: `HUB_URL` needs the `wss://` (or `ws://` for a plaintext local test) scheme and the `/agent/ws` path exactly as shown in the Add Server dialog; or the hub is behind a reverse proxy that isn't forwarding `Upgrade`/`Connection` headers (see the nginx config above — `proxy_set_header Upgrade`/`Connection "upgrade"` is what makes the WebSocket work through it at all, not just plain HTTP).

**Lost the install token before running it.** It's shown exactly once and never stored in plaintext — remove that server from the Servers page and add it again to get a fresh one. The half-installed agent on the old box is harmless; it just never had a valid token to connect with.

**A client's `.ovpn` file "works" on the wrong server, or doesn't work at all.** It can't — that's the point. A cert issued on one server's CA is rejected outright by any other server's OpenVPN daemon (different CA, different trust store). If a download seems to be for the wrong server, you're almost certainly looking at the wrong entry in the server switcher.

</details>

## Security notes

- Single shared admin password on the hub, no per-user accounts, by design — this is sized for "one person or a tiny trusted team," not a multi-tenant SaaS. If you need real RBAC, this isn't it.
- Independent PKI per server is a security boundary, not just an implementation detail: even a hub compromise doesn't hand over every server's CA at once, since the hub never holds any of them.
- A server's backup file contains that server's CA private key. Anyone with it can mint valid client certificates *for that server*. Store it encrypted, or at minimum somewhere with the same access control as that server's root password.
- Static IP assignment is an admin-only action — there is no UI or API path that lets a client choose or change their own pinned IP. If you're building IP-based access control on top of this, that boundary is what makes it trustworthy.
- The management interface (`127.0.0.1:<port>` on each agent) is unauthenticated by OpenVPN's own design — it's protected entirely by being bound to loopback. Never change that bind address.
- Put the hub behind a real TLS certificate (see Reverse proxy + TLS) if it's reachable from the internet at all. Session cookies are `same_site=lax`, not `secure`-only, specifically so it still works over a plain HTTP LAN tunnel — don't run it over HTTP on the public internet.

## License

MIT — see [LICENSE](LICENSE).
