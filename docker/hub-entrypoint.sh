#!/bin/bash
# The hub has no systemd/PID-1 layer (see hub.Dockerfile) — this script IS
# the container's main process setup, run directly, not via a *-first-boot
# systemd unit like the agent image. First-run secret generation is guarded
# by the presence of config.env on the /etc/ovpn-dashboard volume, so it
# survives container recreation exactly like the agent's PKI marker does.
set -euo pipefail

CONFIG=/etc/ovpn-dashboard/config.env

if [ ! -f "$CONFIG" ]; then
  echo "[hub-entrypoint] First run — generating secrets..."
  admin_password="${ADMIN_PASSWORD:-}"
  secrets="$(python3 - "$admin_password" <<'PYEOF'
import hashlib, secrets, sys

password = sys.argv[1] or secrets.token_urlsafe(9)
salt = secrets.token_bytes(16)
digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
print(f"ADMIN_PASSWORD={password}")
print(f"SESSION_SECRET={secrets.token_hex(32)}")
print(f"ADMIN_PASSWORD_SALT={salt.hex()}")
print(f"ADMIN_PASSWORD_HASH={digest.hex()}")
PYEOF
)"
  admin_password="$(echo "$secrets" | grep '^ADMIN_PASSWORD=' | cut -d= -f2-)"
  echo "$secrets" | grep -v '^ADMIN_PASSWORD=' > "$CONFIG"
  chmod 600 "$CONFIG"

  echo "=================================================================="
  echo " ovpn-dashboard hub: first-boot setup complete"
  echo ""
  echo "   Admin password: ${admin_password}"
  echo ""
  echo " This is shown once. From here on only a salted hash is stored —"
  echo " change it from Settings -> Account right after you log in."
  echo "=================================================================="
fi

set -a
# shellcheck disable=SC1090
source "$CONFIG"
set +a

exec /opt/ovpn-dashboard/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
