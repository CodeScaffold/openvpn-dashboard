#!/bin/bash
# Runs on every agent container start (via agent-first-boot.service). PKI
# generation happens exactly once, guarded by the marker file below, which
# lives on the /etc/ovpn-dashboard volume so it survives container
# recreation. Everything else here (user, sudoers, ownership, service
# enablement) is cheap and idempotent, and re-runs every boot on purpose —
# none of it persists in the container's own writable layer, only in the
# volumes. Mirrors agent/install.sh's bare-metal steps; keep the two in sync.
set -euo pipefail

id ovpndash &>/dev/null || useradd --system --home /opt/ovpn-dashboard --shell /usr/sbin/nologin ovpndash
usermod -aG systemd-journal ovpndash

mkdir -p /etc/openvpn/ccd /etc/openvpn/clients /etc/openvpn/server /etc/ovpn-dashboard

# Narrow wrapper so the sudoers grant needs no wildcards — some sudo implementations
# (e.g. sudo-rs, default on newer Ubuntu) reject wildcards embedded within a single
# argument like openvpn-server@*. Must stay in sync with agent/endpoints.py:NAME_RE.
cat > /usr/local/bin/ovpn-endpoint-ctl <<'CTLEOF'
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
CTLEOF
chmod 755 /usr/local/bin/ovpn-endpoint-ctl

# Same rationale, one POSTROUTING NAT rule per endpoint subnet instead of a wildcard.
# Called by agent/endpoints.py on every endpoint create/delete, and re-run for every
# existing endpoint on every agent start (see agent/main.py) since a container
# restart starts with an empty runtime iptables table.
cat > /usr/local/bin/ovpn-nat-ctl <<'CTLEOF'
#!/bin/bash
set -euo pipefail
action="${1:-}"
subnet="${2:-}"
mode="${3:-}"
target="${4:-}"

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

# ovpn-agent.service references this via ExecStartPre=+ (see docker/ovpn-agent.service,
# shared verbatim with the bare-metal install) -- must exist even when BIND_IP is unset
# on this particular container, or that ExecStartPre fails with 203/EXEC. The agent
# process also calls it directly (via sudo, with the IP as an argument) periodically to
# self-heal -- see agent/endpoints.py:ensure_bind_ip. Not a substitute for adding the IP
# to the box's own persistent network config; see the README's "Multiple exit IPs on one
# box" section for why that's still required.
cat > /usr/local/bin/ovpn-bind-ip-ctl <<'CTLEOF'
#!/bin/bash
set -euo pipefail
BIND_IP="${1:-${BIND_IP:-}}"
[ -z "$BIND_IP" ] && exit 0
default_iface="$(ip route show default 2>/dev/null | awk '{print $5; exit}')"
default_iface="${default_iface:-eth0}"
ip addr show dev "$default_iface" | grep -q " ${BIND_IP}/32 " || ip addr add "${BIND_IP}/32" dev "$default_iface"
CTLEOF
chmod 755 /usr/local/bin/ovpn-bind-ip-ctl

cat > /etc/sudoers.d/ovpndash <<'SUDOEOF'
ovpndash ALL=(root) NOPASSWD: /usr/local/bin/ovpn-endpoint-ctl
ovpndash ALL=(root) NOPASSWD: /usr/local/bin/ovpn-nat-ctl
ovpndash ALL=(root) NOPASSWD: /usr/local/bin/ovpn-bind-ip-ctl
ovpndash ALL=(root) NOPASSWD: /usr/sbin/ufw allow *
ovpndash ALL=(root) NOPASSWD: /usr/sbin/ufw delete allow *
SUDOEOF
chmod 440 /etc/sudoers.d/ovpndash

sysctl -w net.ipv4.ip_forward=1 >/dev/null

MARKER=/etc/ovpn-dashboard/.initialized
if [ ! -f "$MARKER" ]; then
  echo "[agent-first-boot] First run — generating this server's own PKI..."

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

  # No endpoint or server.conf is created here — unlike the old single-box
  # install, that's now a normal admin action (Endpoints -> Create endpoint)
  # from the dashboard once this agent shows up online, identical for the
  # first endpoint as for the tenth.

  # NAT is set up per-endpoint at create/delete time instead of one blanket rule
  # here (see agent/endpoints.py:_ensure_nat and ovpn-nat-ctl above) -- that's what
  # lets more than one agent instance share this host's network namespace, each
  # only ever touching its own endpoints' subnets.

  {
    echo "HUB_URL=${HUB_URL:?HUB_URL must be set -- pass -e HUB_URL=wss://your-panel/agent/ws}"
    echo "AGENT_TOKEN=${AGENT_TOKEN:?AGENT_TOKEN must be set -- from the dashboard Add Server dialog}"
    [ -n "${BIND_IP:-}" ] && echo "BIND_IP=${BIND_IP}" || true
  } > /etc/ovpn-dashboard/agent.env
  chmod 600 /etc/ovpn-dashboard/agent.env

  touch "$MARKER"
  echo "[agent-first-boot] Done. Check the dashboard's Servers page — this box should show online shortly."
fi

chown -R ovpndash:ovpndash /etc/openvpn/easy-rsa /etc/openvpn/ccd /etc/openvpn/clients /etc/openvpn/server /etc/ovpn-dashboard
chmod 600 /etc/openvpn/server/server.key /etc/ovpn-dashboard/agent.env 2>/dev/null || true

systemctl enable --now ovpn-agent
