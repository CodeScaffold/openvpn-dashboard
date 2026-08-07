# Runs systemd as PID 1 so the app's own service-management code (systemctl,
# journalctl, the sudoers grants documented in the README) works completely
# unchanged, identically to a bare-metal install — see docker/first-boot.sh
# for the one-time setup that replaces the README's manual steps.
FROM ubuntu:24.04

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
COPY app ./app
COPY static ./static
COPY requirements.txt ./

RUN python3 -m venv venv && venv/bin/pip install --no-cache-dir -r requirements.txt

COPY docker/first-boot.sh /usr/local/bin/first-boot.sh
COPY docker/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY docker/first-boot.service /etc/systemd/system/first-boot.service
COPY docker/ovpn-dashboard.service /etc/systemd/system/ovpn-dashboard.service
RUN chmod +x /usr/local/bin/first-boot.sh /usr/local/bin/docker-entrypoint.sh \
    && systemctl enable first-boot.service \
    && mkdir -p /etc/openvpn/easy-rsa \
    && cp -r /usr/share/easy-rsa/* /etc/openvpn/easy-rsa/

STOPSIGNAL SIGRTMIN+3
VOLUME ["/sys/fs/cgroup"]

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
