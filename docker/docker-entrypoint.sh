#!/bin/bash
# `docker run -e` env vars only reach PID 1 (systemd, started below) — systemd
# does not propagate its own environment to the services it spawns. Persist
# the ones first-boot.sh needs into a file it reads via EnvironmentFile=.
set -eu

{
  echo "VPN_ENDPOINT=${VPN_ENDPOINT:-}"
  echo "ADMIN_PASSWORD=${ADMIN_PASSWORD:-}"
  echo "OPENVPN_PORT=${OPENVPN_PORT:-1194}"
  echo "OPENVPN_PROTO=${OPENVPN_PROTO:-udp}"
} > /etc/docker-env

exec /sbin/init
