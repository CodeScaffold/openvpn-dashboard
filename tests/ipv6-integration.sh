#!/bin/bash
# Run as root on a Linux test host with OpenVPN, iproute2, iptables and tc.
# All network changes live in three temporary namespaces; no live VPN is touched.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TASK_TMP="$(mktemp -d /tmp/ovpn-ipv6-integration.XXXXXX)"
chmod 755 "$TASK_TMP"
PREFIX="ov6-$$"
CLIENT="$PREFIX-c"
SERVER="$PREFIX-s"
INTERNET="$PREFIX-i"
PYTHON="${PYTHON:-python3}"
export PYTHONPATH="$ROOT"
cleanup() {
  for ns in "$CLIENT" "$SERVER" "$INTERNET"; do
    if ip netns list | awk '{print $1}' | grep -qx "$ns"; then
      for pid in $(ip netns pids "$ns"); do kill "$pid" 2>/dev/null || true; done
      ip netns del "$ns"
    fi
  done
  if [[ "${TEST_OK:-0}" == 1 ]]; then
    rm -rf "$TASK_TMP"
  else
    echo "Test logs retained at $TASK_TMP" >&2
    tail -30 "$TASK_TMP/server.log" "$TASK_TMP/client.log" 2>/dev/null || true
  fi
}
trap cleanup EXIT
for ns in "$CLIENT" "$SERVER" "$INTERNET"; do ip netns add "$ns"; ip -n "$ns" link set lo up; done
ip link add ov6c type veth peer name ov6s
ip link set ov6c netns "$CLIENT"
ip link set ov6s netns "$SERVER"
ip -n "$CLIENT" link set ov6c name lan6
ip -n "$SERVER" link set ov6s name lan6
ip -n "$CLIENT" addr add 172.30.254.2/30 dev lan6
ip -n "$SERVER" addr add 172.30.254.1/30 dev lan6
ip -n "$CLIENT" -6 addr add fd99::2/64 dev lan6 nodad
ip -n "$SERVER" -6 addr add fd99::1/64 dev lan6 nodad
ip -n "$CLIENT" link set lan6 up
ip -n "$SERVER" link set lan6 up
ip -n "$CLIENT" route add default via 172.30.254.1
ip -n "$CLIENT" -6 route add default via fd99::1
ip link add ov6w type veth peer name ov6i
ip link set ov6w netns "$SERVER"
ip link set ov6i netns "$INTERNET"
ip -n "$SERVER" link set ov6w name wan6
ip -n "$INTERNET" link set ov6i name wan6
ip -n "$SERVER" addr add 203.0.113.1/24 dev wan6
ip -n "$INTERNET" addr add 203.0.113.2/24 dev wan6
ip -n "$SERVER" -6 addr add 2001:4860:ffff:1::1/64 dev wan6 nodad
ip -n "$INTERNET" -6 addr add 2001:4860:ffff:1::2/64 dev wan6 nodad
ip -n "$INTERNET" -6 addr add 2606:4700:ffff::2/128 dev lo nodad
ip -n "$SERVER" link set wan6 up
ip -n "$INTERNET" link set wan6 up
ip -n "$SERVER" route add default via 203.0.113.2
ip -n "$SERVER" -6 route add default via 2001:4860:ffff:1::2
ip -n "$INTERNET" route add default via 203.0.113.1
ip netns exec "$SERVER" sysctl -w net.ipv4.ip_forward=1 >/dev/null
ip netns exec "$SERVER" iptables -t nat -A POSTROUTING -s 10.9.200.0/24 -o wan6 -j MASQUERADE
ip netns exec "$SERVER" ip6tables -P FORWARD DROP
cd "$TASK_TMP"
openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.crt -days 1 -subj /CN=ovpn-ipv6-test-ca >/dev/null 2>&1
for role in server client; do
  openssl req -newkey rsa:2048 -nodes -keyout "$role.key" -out "$role.csr" -subj "/CN=ci-$role" >/dev/null 2>&1
  printf 'keyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=%sAuth\n' "$role" > "$role.ext"
  openssl x509 -req -in "$role.csr" -CA ca.crt -CAkey ca.key -CAcreateserial -out "$role.crt" -days 1 -extfile "$role.ext" >/dev/null 2>&1
done
openvpn --genkey secret tls-crypt.key
export IPV6_EGRESS=2001:4860:ffff:1::1
unset BIND_IP
export TASK_TMP
"$PYTHON" - <<'PY'
import os
from pathlib import Path
from agent import endpoints, ipv6, netlimit
root = Path(os.environ['TASK_TMP'])
endpoints.SERVER_DIR = root
config = endpoints._config_text('server', 1194, 'udp', 'AES-128-GCM', 'tun0', '10.9.200.0/24', '', '', 7505, str(root/'ccd'/'server'))
# Test certificates are disposable; isolate routing from PKI/service permissions.
config = '\n'.join(line for line in config.splitlines() if not line.startswith(('crl-verify ', 'user ', 'group ', 'status '))) + '\n'
(root/'server.conf').write_text(config)
(root/'network6').write_text(str(ipv6.network('10.9.200.0/24')))
netlimit.write_ccd(str(root/'ccd'/'server'), 'ci-client', '10.9.200.50', False)
PY
cat > client.conf <<EOF
client
dev tun0
proto udp
remote 172.30.254.1 1194
nobind
remote-cert-tls server
ca $TASK_TMP/ca.crt
cert $TASK_TMP/client.crt
key $TASK_TMP/client.key
tls-crypt $TASK_TMP/tls-crypt.key
data-ciphers AES-128-GCM
verb 3
EOF
cat > echo-server.py <<'PY'
import http.server, socket, threading
class Echo(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(self.client_address[0].encode())
    def log_message(self, *args): pass
class V6(http.server.ThreadingHTTPServer): address_family = socket.AF_INET6
threading.Thread(target=http.server.ThreadingHTTPServer(('203.0.113.2', 8080), Echo).serve_forever, daemon=True).start()
V6(('2606:4700:ffff::2', 8080), Echo).serve_forever()
PY
ip netns exec "$INTERNET" "$PYTHON" echo-server.py > echo.log 2>&1 &
ip netns exec "$SERVER" bash "$ROOT/scripts/ovpn-ipv6-ctl" add tun0 "$(cat network6)" "$IPV6_EGRESS"
ip netns exec "$SERVER" bash "$ROOT/scripts/ovpn-ipv6-ctl" add tun0 "$(cat network6)" "$IPV6_EGRESS"
[[ "$(ip netns exec "$SERVER" ip6tables -t nat -S POSTROUTING | grep -c -- '-j SNAT')" == 1 ]]
start_vpn() {
  ip netns exec "$SERVER" openvpn --config server.conf --daemon --writepid "$TASK_TMP/server.pid" --log-append "$TASK_TMP/server.log"
  ip netns exec "$CLIENT" openvpn --config client.conf --daemon --writepid "$TASK_TMP/client.pid" --log-append "$TASK_TMP/client.log"
  for attempt in $(seq 1 30); do
    if grep -q 'Initialization Sequence Completed' client.log; then return; fi
    sleep 1
  done
  echo 'Client did not connect' >&2
  exit 1
}
start_vpn
sleep 2
ip -n "$CLIENT" route get 203.0.113.2 | grep -q 'dev tun0'
ip -n "$CLIENT" -6 route get 2606:4700:ffff::2 | grep -q 'dev tun0'
[[ "$(ip netns exec "$CLIENT" curl --noproxy '*' -fsS --max-time 10 http://203.0.113.2:8080/)" == 203.0.113.1 ]]
[[ "$(ip netns exec "$CLIENT" curl --noproxy '*' -g -fsS --max-time 10 'http://[2606:4700:ffff::2]:8080/')" == "$IPV6_EGRESS" ]]
echo 'PASS: IPv4 and IPv6 traverse OpenVPN; IPv6 exits through configured SNAT address.'
ip netns exec "$SERVER" "$PYTHON" - <<'PY'
import os
from pathlib import Path
from agent import db, endpoints, netlimit
endpoints.SERVER_DIR = Path(os.environ['TASK_TMP'])
db.get_all_endpoints = lambda: [{'tun_dev': 'tun0', 'name': 'server'}]
netlimit.apply_bandwidth('tun0', '10.9.200.50', 10000)
PY
ip netns exec "$SERVER" tc filter show dev tun0 parent 1: | grep -q 'protocol ipv6'
ip netns exec "$SERVER" tc filter show dev tun0 parent ffff: | grep -q 'protocol ipv6'
[[ "$(ip netns exec "$CLIENT" curl --noproxy '*' -g -fsS --max-time 10 'http://[2606:4700:ffff::2]:8080/')" == "$IPV6_EGRESS" ]]
echo 'PASS: Real IPv6 tc filters coexist with IPv4 and traffic still works.'
kill "$(cat client.pid)" "$(cat server.pid)"
sleep 2
: > client.log
: > server.log
IPV6_EGRESS= "$PYTHON" - <<'PY'
from pathlib import Path
from agent import ipv6
path = Path('server.conf')
path.write_text(ipv6.upgrade_config(path.read_text(), '10.9.200.0/24'))
PY
start_vpn
sleep 2
ip -n "$CLIENT" -6 route get 2606:4700:ffff::2 | grep -q 'dev tun0'
if ip netns exec "$CLIENT" curl --noproxy '*' -g -fsS --max-time 5 'http://[2606:4700:ffff::2]:8080/' > /dev/null 2>&1; then
  echo 'IPv6 block test unexpectedly succeeded' >&2
  exit 1
fi
[[ "$(ip netns exec "$CLIENT" curl --noproxy '*' -fsS --max-time 10 http://203.0.113.2:8080/)" == 203.0.113.1 ]]
echo 'PASS: IPv4-only fallback captures and rejects IPv6; IPv4 remains usable.'
ip netns exec "$SERVER" bash "$ROOT/scripts/ovpn-ipv6-ctl" remove tun0 "$(cat network6)" "$IPV6_EGRESS"
ip netns exec "$SERVER" bash "$ROOT/scripts/ovpn-ipv6-ctl" remove tun0 "$(cat network6)" "$IPV6_EGRESS"
[[ "$(ip netns exec "$SERVER" ip6tables -t nat -S POSTROUTING | grep -c -- '-j SNAT' || true)" == 0 ]]
echo 'PASS: IPv6 rule setup/removal is idempotent.'
TEST_OK=1
