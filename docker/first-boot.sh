#!/bin/bash
# Runs on every container start (via first-boot.service). PKI/config generation
# happens exactly once, guarded by the marker file below, which lives on the
# /etc/ovpn-dashboard volume so it survives container recreation. Everything
# else here (user, sudoers, ownership, service enablement) is cheap and
# idempotent, and re-runs every boot on purpose — none of it persists in the
# container's own writable layer, only in the volumes.
set -euo pipefail

id ovpndash &>/dev/null || useradd --system --home /opt/ovpn-dashboard --shell /usr/sbin/nologin ovpndash
usermod -aG systemd-journal ovpndash

mkdir -p /etc/openvpn/ccd /etc/openvpn/clients /etc/openvpn/server /etc/ovpn-dashboard

cat > /etc/sudoers.d/ovpndash <<'SUDOEOF'
ovpndash ALL=(root) NOPASSWD: /usr/bin/systemctl restart openvpn-server@server
ovpndash ALL=(root) NOPASSWD: /usr/bin/systemctl enable --now openvpn-server@*
ovpndash ALL=(root) NOPASSWD: /usr/bin/systemctl disable --now openvpn-server@*
ovpndash ALL=(root) NOPASSWD: /usr/bin/systemctl disable openvpn-server@*
ovpndash ALL=(root) NOPASSWD: /usr/sbin/ufw allow *
ovpndash ALL=(root) NOPASSWD: /usr/sbin/ufw delete allow *
SUDOEOF
chmod 440 /etc/sudoers.d/ovpndash

sysctl -w net.ipv4.ip_forward=1 >/dev/null

MARKER=/etc/ovpn-dashboard/.initialized
if [ ! -f "$MARKER" ]; then
  echo "[first-boot] First run — generating PKI and config..."

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

  default_iface="$(ip route show default 2>/dev/null | awk '{print $5; exit}')"
  default_iface="${default_iface:-eth0}"

  cat > /etc/openvpn/server/server.conf <<EOF
port ${OPENVPN_PORT:-1194}
proto ${OPENVPN_PROTO:-udp}
dev tun
ca ca.crt
cert server.crt
key server.key
tls-crypt tls-crypt.key
dh none
ecdh-curve prime256v1
topology subnet
server 10.9.0.0 255.255.255.0
ifconfig-pool-persist ipp.txt
push "redirect-gateway def1 bypass-dhcp"
push "dhcp-option DNS 1.1.1.1"
push "dhcp-option DNS 1.0.0.1"
keepalive 10 120
cipher AES-128-GCM
data-ciphers AES-128-GCM
auth SHA256
user nobody
group nogroup
persist-key
persist-tun
status /var/log/openvpn/status.log
verb 3
explicit-exit-notify 1
crl-verify crl.pem
management 127.0.0.1 7505
client-config-dir /etc/openvpn/ccd
duplicate-cn
max-clients 50
EOF

  iptables -t nat -C POSTROUTING -s 10.9.0.0/24 -o "$default_iface" -j MASQUERADE 2>/dev/null || \
  iptables -t nat -A POSTROUTING -s 10.9.0.0/24 -o "$default_iface" -j MASQUERADE

  admin_password="${ADMIN_PASSWORD:-$(openssl rand -base64 12)}"
  secrets="$(python3 - "$admin_password" <<'PYEOF'
import hashlib, secrets, sys
password = sys.argv[1]
salt = secrets.token_bytes(16)
digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
print(f"SESSION_SECRET={secrets.token_hex(32)}")
print(f"ADMIN_PASSWORD_SALT={salt.hex()}")
print(f"ADMIN_PASSWORD_HASH={digest.hex()}")
PYEOF
)"
  {
    echo "$secrets"
    echo "VPN_ENDPOINT=${VPN_ENDPOINT:?VPN_ENDPOINT must be set — pass -e VPN_ENDPOINT=your.domain.or.ip}"
  } > /etc/ovpn-dashboard/config.env
  chmod 600 /etc/ovpn-dashboard/config.env

  touch "$MARKER"

  echo "=================================================================="
  echo " ovpn-dashboard: first-boot setup complete"
  echo ""
  echo "   Admin password: ${admin_password}"
  echo ""
  echo " This is shown once. From here on only a salted hash is stored —"
  echo " change it from Settings -> Account right after you log in."
  echo "=================================================================="
fi

chown -R ovpndash:ovpndash /etc/openvpn/easy-rsa /etc/openvpn/ccd /etc/openvpn/clients /etc/openvpn/server /etc/ovpn-dashboard
chmod 600 /etc/openvpn/server/server.key /etc/ovpn-dashboard/config.env 2>/dev/null || true

systemctl enable --now openvpn-server@server
systemctl enable --now ovpn-dashboard
