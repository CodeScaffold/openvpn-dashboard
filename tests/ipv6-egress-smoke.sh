#!/bin/bash
# Real internet test through a disposable VPN listener and a namespace client.
# Requires root; temporarily adds ONLY test-specific firewall/interface rules.
# Never restarts existing endpoints, modifies their PKI, or flushes tables.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
: "${IPV6_EGRESS:?Set IPV6_EGRESS to an IPv6 address already on this host}"
PYTHON="${PYTHON:-python3}"
export PYTHONPATH="$ROOT"
TASK_TMP="$(mktemp -d /tmp/ovpn-ipv6-egress.XXXXXX)"
NS="ov6live-$$"
TEST_LINK="ov6l$$"
SUBNET=10.9.240.0/24
PORT=41194
IFACE=tun240
UPLINK="$(ip -4 route show default | awk '{print $5; exit}')"
IP6_UPLINK="$(ip -6 route show default | awk '{print $5; exit}')"
FORWARDING="$(sysctl -n net.ipv6.conf.all.forwarding)"
ACCEPT_RA="$(sysctl -n "net.ipv6.conf.${IP6_UPLINK}.accept_ra")"
export TASK_TMP
NETWORK6="$($PYTHON -c 'from agent import ipv6; print(ipv6.network("10.9.240.0/24"))')"
[[ "$IFACE" == tun240 ]]
if ip link show "$IFACE" >/dev/null 2>&1 || ip route show "$SUBNET" | grep -q .; then
  echo 'Test subnet/interface already in use; aborting.' >&2
  exit 1
fi
cleanup() {
  set +e
  if [[ -f "$TASK_TMP/server.pid" ]]; then kill "$(cat "$TASK_TMP/server.pid")" 2>/dev/null; fi
  for pid in $(ip netns pids "$NS" 2>/dev/null); do kill "$pid" 2>/dev/null; done
  ip netns del "$NS" 2>/dev/null
  ip link del "$TEST_LINK" 2>/dev/null
  iptables -w 5 -D INPUT -i "$TEST_LINK" -p udp --dport "$PORT" -j ACCEPT 2>/dev/null
  iptables -w 5 -D FORWARD -i "$IFACE" -o "$UPLINK" -s "$SUBNET" -j ACCEPT 2>/dev/null
  iptables -w 5 -D FORWARD -i "$UPLINK" -o "$IFACE" -d "$SUBNET" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null
  iptables -w 5 -t nat -D POSTROUTING -s "$SUBNET" -o "$UPLINK" -j MASQUERADE 2>/dev/null
  bash "$ROOT/scripts/ovpn-ipv6-ctl" remove "$IFACE" "$NETWORK6" "$IPV6_EGRESS"
  sysctl -w "net.ipv6.conf.${IP6_UPLINK}.accept_ra=$ACCEPT_RA" >/dev/null
  sysctl -w "net.ipv6.conf.all.forwarding=$FORWARDING" >/dev/null
  if [[ "${TEST_OK:-0}" == 1 ]]; then
    rm -rf "$TASK_TMP"
  else
    echo "Logs retained at $TASK_TMP" >&2
    tail -25 "$TASK_TMP/server.log" "$TASK_TMP/client.log" 2>/dev/null
  fi
}
trap cleanup EXIT
ip netns add "$NS"
ip -n "$NS" link set lo up
ip link add "$TEST_LINK" type veth peer name ov6peer
ip link set ov6peer netns "$NS"
ip addr add 172.31.254.1/30 dev "$TEST_LINK"
ip -n "$NS" addr add 172.31.254.2/30 dev ov6peer
ip link set "$TEST_LINK" up
ip -n "$NS" link set ov6peer up
ip -n "$NS" route add default via 172.31.254.1
iptables -w 5 -I INPUT 1 -i "$TEST_LINK" -p udp --dport "$PORT" -j ACCEPT
iptables -w 5 -I FORWARD 1 -i "$IFACE" -o "$UPLINK" -s "$SUBNET" -j ACCEPT
iptables -w 5 -I FORWARD 1 -i "$UPLINK" -o "$IFACE" -d "$SUBNET" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
iptables -w 5 -t nat -I POSTROUTING 1 -s "$SUBNET" -o "$UPLINK" -j MASQUERADE
bash "$ROOT/scripts/ovpn-ipv6-ctl" add "$IFACE" "$NETWORK6" "$IPV6_EGRESS"
cd "$TASK_TMP"
openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.crt -days 1 -subj /CN=ovpn-ipv6-smoke-ca >/dev/null 2>&1
for role in server client; do
  openssl req -newkey rsa:2048 -nodes -keyout "$role.key" -out "$role.csr" -subj "/CN=smoke-$role" >/dev/null 2>&1
  printf 'keyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=%sAuth\n' "$role" > "$role.ext"
  openssl x509 -req -in "$role.csr" -CA ca.crt -CAkey ca.key -CAcreateserial -out "$role.crt" -days 1 -extfile "$role.ext" >/dev/null 2>&1
done
openvpn --genkey secret tls-crypt.key
"$PYTHON" - <<'PY'
import os
from pathlib import Path
from unittest.mock import patch
from agent import endpoints
with patch.object(endpoints, 'BIND_IP', '172.31.254.1'):
    config = endpoints._config_text('smoke', 41194, 'udp', 'AES-128-GCM', 'tun240', '10.9.240.0/24', '', '', 7599, str(Path.cwd()/'ccd'))
config = '\n'.join(line for line in config.splitlines() if not line.startswith(('crl-verify ', 'user ', 'group ', 'status ', 'management '))) + '\n'
Path('server.conf').write_text(config)
Path('ccd').mkdir()
PY
cat > client.conf <<EOF
client
dev tun0
proto udp
remote 172.31.254.1 $PORT
nobind
remote-cert-tls server
ca $TASK_TMP/ca.crt
cert $TASK_TMP/client.crt
key $TASK_TMP/client.key
tls-crypt $TASK_TMP/tls-crypt.key
data-ciphers AES-128-GCM
verb 3
EOF
openvpn --config server.conf --daemon --writepid "$TASK_TMP/server.pid" --log "$TASK_TMP/server.log"
ip netns exec "$NS" openvpn --config client.conf --daemon --writepid "$TASK_TMP/client.pid" --log "$TASK_TMP/client.log"
for attempt in $(seq 1 30); do
  if grep -q 'Initialization Sequence Completed' client.log; then break; fi
  sleep 1
done
grep -q 'Initialization Sequence Completed' client.log
sleep 2
mapfile -t TARGETS < <("$PYTHON" - <<'PY'
import socket
for family in (socket.AF_INET, socket.AF_INET6):
    print(socket.getaddrinfo('api64.ipify.org', 443, family, socket.SOCK_STREAM)[0][4][0])
PY
)
ip -n "$NS" route get "${TARGETS[0]}" | grep -q 'dev tun0'
ip -n "$NS" -6 route get "${TARGETS[1]}" | grep -q 'dev tun0'
EXIT4="$(ip netns exec "$NS" curl --noproxy '*' -4 -fsS --max-time 20 --resolve "api64.ipify.org:443:${TARGETS[0]}" https://api64.ipify.org/)"
EXIT6="$(ip netns exec "$NS" curl --noproxy '*' -6 -fsS --max-time 20 --resolve "api64.ipify.org:443:[${TARGETS[1]}]" https://api64.ipify.org/)"
[[ "$EXIT6" == "$IPV6_EGRESS" ]]
echo "PASS: Temporary VPN client reaches real internet: IPv4=$EXIT4 IPv6=$EXIT6"
TEST_OK=1
