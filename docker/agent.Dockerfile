# Runs systemd as PID 1 so the agent's own service-management code (systemctl,
# journalctl, the sudoers grants documented in the README) works completely
# unchanged, identically to a bare-metal install — see agent-first-boot.sh
# for the one-time setup that replaces agent/install.sh's steps. Build
# context is the repo root: docker build -f docker/agent.Dockerfile .
#
# 26.04, not 24.04: OpenVPN's kernel data channel offload (DCO) needs a recent
# enough userspace OpenVPN to negotiate with the host's in-kernel `ovpn` module
# over netlink -- 24.04's OpenVPN 2.6.19 silently falls back to slow userspace
# packet processing even when the host kernel has the module loaded (same
# kernel, since this runs --network host), while 26.04's 2.7.0 works. This is
# a large, measured difference -- roughly 3x throughput on real hardware.
FROM ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        systemd systemd-sysv dbus \
        openvpn easy-rsa \
        python3 python3-venv python3-pip \
        iptables iproute2 sudo ufw openssl ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && systemctl enable dbus.service dbus.socket

# Standard "systemd in Docker" trimming: mask units that fail or don't make
# sense in a container (they'd otherwise slow down or block boot).
RUN systemctl mask \
        systemd-udevd.service systemd-udevd-kernel.socket systemd-udevd-control.socket \
        systemd-networkd.service systemd-resolved.service \
        getty.target getty-static.service \
    || true

WORKDIR /opt/ovpn-dashboard
COPY agent ./agent
COPY common ./common
COPY requirements.txt ./

RUN python3 -m venv venv && venv/bin/pip install --no-cache-dir -r requirements.txt

COPY docker/agent-first-boot.sh /usr/local/bin/agent-first-boot.sh
COPY docker/agent-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY docker/agent-first-boot.service /etc/systemd/system/agent-first-boot.service
COPY docker/ovpn-agent.service /etc/systemd/system/ovpn-agent.service
RUN chmod +x /usr/local/bin/agent-first-boot.sh /usr/local/bin/docker-entrypoint.sh \
    && systemctl enable agent-first-boot.service \
    && mkdir -p /etc/openvpn/easy-rsa \
    && cp -r /usr/share/easy-rsa/* /etc/openvpn/easy-rsa/

STOPSIGNAL SIGRTMIN+3
VOLUME ["/sys/fs/cgroup"]

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
