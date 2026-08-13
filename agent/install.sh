#!/bin/bash
# Automated installer for a single ovpn-dashboard agent. Run on the Linux box
# you want to add as a VPN exit — it does NOT touch the hub (dashboard) at
# all. Mirrors the README's manual agent-install steps exactly; read the
# README first if you want to understand what this does before running it.
#
# Usage (this is exactly the command the dashboard's "Add server" dialog
# shows you, with your own token filled in):
#
#   curl -fsSL https://raw.githubusercontent.com/CodeScaffold/openvpn-dashboard/main/agent/install.sh \
#     | sudo HUB_URL=wss://panel.example.com/agent/ws AGENT_TOKEN=<token> bash
#
# Required env vars:
#   HUB_URL      wss://<your-dashboard-host>/agent/ws — the token dialog shows this pre-filled
#   AGENT_TOKEN  the one-time enrollment token from that same dialog
#
# Optional env vars:
#   INSTALL_DIR  where the code lands (default /opt/ovpn-dashboard)
#   REPO_URL     git remote to clone (default the public repo)
#   REPO_REF     branch/tag to check out (default main)
#   BIND_IP      only needed when running more than one agent on the same box (e.g.
#                Hetzner floating IPs) -- pins this agent's OpenVPN listeners and its
#                clients' NAT/exit traffic to exactly this address instead of the
#                box's default one. Leave unset for the ordinary one-agent-per-box case.
#   SWAP_GB      size of a swapfile to create if this box has none (default 2). Cheap
#                insurance against an OOM kill on a small VPS. Set to 0 to skip, or just
#                delete /swapfile and its /etc/fstab line afterward if you change your mind.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run this as root (or with sudo) — it installs system packages and a systemd unit." >&2
  exit 1
fi

: "${HUB_URL:?HUB_URL is required — e.g. HUB_URL=wss://panel.example.com/agent/ws}"
: "${AGENT_TOKEN:?AGENT_TOKEN is required — from the dashboard Add Server dialog}"
INSTALL_DIR="${INSTALL_DIR:-/opt/ovpn-dashboard}"
REPO_URL="${REPO_URL:-https://github.com/CodeScaffold/openvpn-dashboard}"
REPO_REF="${REPO_REF:-main}"

echo "==> Installing base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
  openvpn easy-rsa python3 python3-venv python3-pip git ufw iptables iproute2 sudo openssl ca-certificates

echo "==> Creating the ovpndash system user"
id ovpndash &>/dev/null || useradd --system --home "$INSTALL_DIR" --shell /usr/sbin/nologin ovpndash
usermod -aG systemd-journal ovpndash

echo "==> Building the local PKI (this server's own CA — never shared with any other server)"
mkdir -p /etc/openvpn/easy-rsa /etc/openvpn/server /etc/openvpn/ccd /etc/openvpn/clients
if [[ ! -f /etc/openvpn/easy-rsa/pki/ca.crt ]]; then
  cp -r /usr/share/easy-rsa/* /etc/openvpn/easy-rsa/
  cd /etc/openvpn/easy-rsa
  cat > vars <<'VARSEOF'
set_var EASYRSA_ALGO ec
set_var EASYRSA_CURVE prime256v1
set_var EASYRSA_CA_EXPIRE 3650
set_var EASYRSA_CERT_EXPIRE 3650
set_var EASYRSA_BATCH 1
VARSEOF
  ./easyrsa init-pki
  ./easyrsa build-ca nopass
  ./easyrsa gen-req server nopass
  ./easyrsa sign-req server server
  openvpn --genkey secret pki/tls-crypt.key
  ./easyrsa gen-crl

  cp pki/ca.crt pki/issued/server.crt pki/private/server.key pki/tls-crypt.key pki/crl.pem /etc/openvpn/server/
  chmod 600 /etc/openvpn/server/server.key
else
  echo "    (PKI already exists, leaving it alone)"
fi

echo "==> Enabling IP forwarding"
echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/99-openvpn.conf
sysctl --system > /dev/null

SWAP_GB="${SWAP_GB:-2}"
if [[ "$SWAP_GB" != "0" && -z "$(swapon --show 2>/dev/null)" ]]; then
  echo "==> Adding a ${SWAP_GB}G swapfile (SWAP_GB=0 to skip; delete /swapfile afterward to remove it)"
  fallocate -l "${SWAP_GB}G" /swapfile
  chmod 600 /swapfile
  mkswap /swapfile > /dev/null
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo 'vm.swappiness=10' > /etc/sysctl.d/99-swappiness.conf
  sysctl --system > /dev/null
else
  echo "==> Swap already present or SWAP_GB=0 -- leaving it alone"
fi

echo "==> Firewall: allow SSH/HTTP/HTTPS, tunnel routing between tun* and the default interface"
default_iface="$(ip route show default 2>/dev/null | awk '{print $5; exit}')"
default_iface="${default_iface:-eth0}"
# NAT (POSTROUTING/MASQUERADE) is deliberately NOT set up here as one blanket rule
# for the whole 10.9.0.0/16 range -- the agent adds/removes one specific rule per
# endpoint at create/delete time instead (see agent/endpoints.py:_ensure_nat), and
# re-asserts them all on every agent start. That's what makes it safe for more than
# one agent instance to share this box, each owning only its own endpoints/subnets.
ufw route allow in on tun+ out on "$default_iface" > /dev/null
ufw route allow in on "$default_iface" out on tun+ > /dev/null
ufw allow 22/tcp comment 'SSH' > /dev/null
yes | ufw enable > /dev/null 2>&1 || true

echo "==> Endpoint-control wrapper (avoids sudoers wildcards some sudo implementations reject)"
cat > /usr/local/bin/ovpn-endpoint-ctl <<'CTLEOF'
#!/bin/bash
# Narrow wrapper so the ovpndash sudoers grant needs no wildcards — some sudo
# implementations (e.g. sudo-rs, default on newer Ubuntu) reject wildcards
# embedded within a single command argument like openvpn-server@*. Validating
# the endpoint name here instead of relying on sudo's own pattern matching is
# also just a better design. Must stay in sync with agent/endpoints.py:NAME_RE.
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
CTLEOF
chmod 755 /usr/local/bin/ovpn-endpoint-ctl

echo "==> NAT-control wrapper (per-endpoint POSTROUTING rule, no sudoers wildcards)"
cat > /usr/local/bin/ovpn-nat-ctl <<'CTLEOF'
#!/bin/bash
# Narrow wrapper, same rationale as ovpn-endpoint-ctl. Adds/removes exactly one
# POSTROUTING rule for one endpoint's tunnel subnet -- called by
# agent/endpoints.py on every endpoint create/delete, and re-run for every
# existing endpoint each time the agent starts (a ufw reload or reboot can
# drop a runtime-only iptables rule; this makes it self-healing).
set -euo pipefail
action="${1:-}"    # add | remove
subnet="${2:-}"    # e.g. 10.9.3.0/24
mode="${3:-}"      # snat | masquerade
target="${4:-}"    # bind IP, only used when mode=snat

if ! [[ "$subnet" =~ ^10\.9\.[0-9]{1,3}\.0/24$ ]]; then
  echo "invalid subnet" >&2
  exit 1
fi

default_iface="$(ip route show default 2>/dev/null | awk '{print $5; exit}')"
default_iface="${default_iface:-eth0}"

case "$mode" in
  snat)
    if ! [[ "$target" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
      echo "invalid target ip" >&2
      exit 1
    fi
    rule=(-s "$subnet" -o "$default_iface" -j SNAT --to-source "$target")
    ;;
  masquerade)
    rule=(-s "$subnet" -o "$default_iface" -j MASQUERADE)
    ;;
  *)
    echo "invalid mode: $mode" >&2
    exit 1
    ;;
esac

case "$action" in
  add)
    iptables -t nat -C POSTROUTING "${rule[@]}" 2>/dev/null || iptables -t nat -A POSTROUTING "${rule[@]}"
    ;;
  remove)
    iptables -t nat -D POSTROUTING "${rule[@]}" 2>/dev/null || true
    ;;
  *)
    echo "unknown action: $action" >&2
    exit 1
    ;;
esac
CTLEOF
chmod 755 /usr/local/bin/ovpn-nat-ctl

echo "==> Bind-IP wrapper (idempotently owns BIND_IP on the NIC)"
cat > /usr/local/bin/ovpn-bind-ip-ctl <<'CTLEOF'
#!/bin/bash
# Ensures BIND_IP is present as a /32 on the default interface -- needed before
# OpenVPN's 'local' bind or the SNAT rule in ovpn-nat-ctl can use it. No-op if
# unset. Takes the IP as $1 if given (how the agent calls it, to self-heal
# periodically without depending on env passthrough through sudo), else falls
# back to $BIND_IP from the environment (how the systemd unit's ExecStartPre=+
# calls it at every agent start, via EnvironmentFile=).
#
# This alone is NOT a persistent fix -- a box that manages its interface via
# DHCP (netplan/systemd-networkd) can silently drop an address that isn't part
# of its own declared config on the next lease renewal. The real fix is adding
# the IP there too; see the README's "Multiple exit IPs on one box" section.
# This script is the belt-and-suspenders fallback for whenever that drifts.
set -euo pipefail
BIND_IP="${1:-${BIND_IP:-}}"
[ -z "$BIND_IP" ] && exit 0
default_iface="$(ip route show default 2>/dev/null | awk '{print $5; exit}')"
default_iface="${default_iface:-eth0}"
ip addr show dev "$default_iface" | grep -q " ${BIND_IP}/32 " || ip addr add "${BIND_IP}/32" dev "$default_iface"
CTLEOF
chmod 755 /usr/local/bin/ovpn-bind-ip-ctl

echo "==> Sudoers: the narrow, explicit grant set the agent needs"
cat > /etc/sudoers.d/ovpndash <<'SUDOEOF'
ovpndash ALL=(root) NOPASSWD: /usr/local/bin/ovpn-endpoint-ctl
ovpndash ALL=(root) NOPASSWD: /usr/local/bin/ovpn-nat-ctl
ovpndash ALL=(root) NOPASSWD: /usr/local/bin/ovpn-bind-ip-ctl
ovpndash ALL=(root) NOPASSWD: /usr/sbin/ufw allow *
ovpndash ALL=(root) NOPASSWD: /usr/sbin/ufw delete allow *
SUDOEOF
visudo -c -f /etc/sudoers.d/ovpndash
chmod 440 /etc/sudoers.d/ovpndash

echo "==> Getting the code"
if [[ -d "$INSTALL_DIR/.git" ]]; then
  git -C "$INSTALL_DIR" fetch --depth 1 origin "$REPO_REF"
  git -C "$INSTALL_DIR" checkout "$REPO_REF"
  git -C "$INSTALL_DIR" reset --hard "origin/$REPO_REF"
else
  git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"
python3 -m venv venv
venv/bin/pip install --quiet --no-cache-dir -r requirements.txt

echo "==> Config"
mkdir -p /etc/ovpn-dashboard
{
  echo "HUB_URL=${HUB_URL}"
  echo "AGENT_TOKEN=${AGENT_TOKEN}"
  [ -n "${BIND_IP:-}" ] && echo "BIND_IP=${BIND_IP}" || true
} > /etc/ovpn-dashboard/agent.env
chmod 600 /etc/ovpn-dashboard/agent.env

chown -R ovpndash:ovpndash "$INSTALL_DIR" /etc/openvpn/easy-rsa /etc/openvpn/ccd /etc/openvpn/clients /etc/openvpn/server /etc/ovpn-dashboard
chmod 600 /etc/openvpn/server/server.key /etc/ovpn-dashboard/agent.env 2>/dev/null || true

echo "==> systemd service"
cat > /etc/systemd/system/ovpn-agent.service <<EOF
[Unit]
Description=ovpn-dashboard agent
After=network-online.target
Wants=network-online.target

[Service]
User=ovpndash
Group=ovpndash
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=/etc/ovpn-dashboard/agent.env
ExecStartPre=+/usr/local/bin/ovpn-bind-ip-ctl
ExecStart=${INSTALL_DIR}/venv/bin/python3 -m agent.main
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

sleep 2
if systemctl is-active --quiet ovpn-agent; then
  echo "=================================================================="
  echo " Agent installed and running."
  echo ""
  echo " It should show online in the dashboard within a few seconds."
  echo " Next: create an endpoint for it from the Endpoints page."
  echo "=================================================================="
else
  echo "=================================================================="
  echo " Agent installed but didn't come up cleanly — check:"
  echo "   journalctl -u ovpn-agent -n 50 --no-pager"
  echo "=================================================================="
  exit 1
fi
