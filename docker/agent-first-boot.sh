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

cat > /etc/sudoers.d/ovpndash <<'SUDOEOF'
ovpndash ALL=(root) NOPASSWD: /usr/local/bin/ovpn-endpoint-ctl
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

  default_iface="$(ip route show default 2>/dev/null | awk '{print $5; exit}')"
  default_iface="${default_iface:-eth0}"
  # /16, not /24: this agent can host several endpoints, each on its own
  # 10.9.<n>.0/24 slice (see agent/endpoints.py) — one NAT rule needs to
  # cover all of them, not just the first.
  iptables -t nat -C POSTROUTING -s 10.9.0.0/16 -o "$default_iface" -j MASQUERADE 2>/dev/null || \
  iptables -t nat -A POSTROUTING -s 10.9.0.0/16 -o "$default_iface" -j MASQUERADE

  {
    echo "HUB_URL=${HUB_URL:?HUB_URL must be set -- pass -e HUB_URL=wss://your-panel/agent/ws}"
    echo "AGENT_TOKEN=${AGENT_TOKEN:?AGENT_TOKEN must be set -- from the dashboard Add Server dialog}"
  } > /etc/ovpn-dashboard/agent.env
  chmod 600 /etc/ovpn-dashboard/agent.env

  touch "$MARKER"
  echo "[agent-first-boot] Done. Check the dashboard's Servers page — this box should show online shortly."
fi

chown -R ovpndash:ovpndash /etc/openvpn/easy-rsa /etc/openvpn/ccd /etc/openvpn/clients /etc/openvpn/server /etc/ovpn-dashboard
chmod 600 /etc/openvpn/server/server.key /etc/ovpn-dashboard/agent.env 2>/dev/null || true

systemctl enable --now ovpn-agent
