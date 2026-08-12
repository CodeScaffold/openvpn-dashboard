#!/bin/bash
# `docker run -e` env vars only reach PID 1 (systemd, started below) — systemd
# does not propagate its own environment to the services it spawns. Persist
# the ones agent-first-boot.sh needs into a file it reads via EnvironmentFile=.
set -eu

{
  echo "HUB_URL=${HUB_URL:-}"
  echo "AGENT_TOKEN=${AGENT_TOKEN:-}"
} > /etc/docker-env

exec /sbin/init
