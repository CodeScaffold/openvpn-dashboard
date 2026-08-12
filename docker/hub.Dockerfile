# The hub is a plain web service now — it never shells out to systemctl/tc/
# ufw or touches /etc/openvpn (that's all agent-side, see agent.Dockerfile).
# No systemd-as-PID-1, no --privileged, no host networking: this is a normal
# container. Build context is the repo root: docker build -f docker/hub.Dockerfile .
FROM python:3.12-slim

RUN useradd --system --home /opt/ovpn-dashboard --shell /usr/sbin/nologin ovpndash

WORKDIR /opt/ovpn-dashboard
COPY app ./app
COPY common ./common
COPY static ./static
COPY requirements.txt ./

RUN python -m venv venv && venv/bin/pip install --no-cache-dir -r requirements.txt

COPY docker/hub-entrypoint.sh /usr/local/bin/hub-entrypoint.sh
RUN chmod +x /usr/local/bin/hub-entrypoint.sh \
    && mkdir -p /etc/ovpn-dashboard \
    && chown -R ovpndash:ovpndash /opt/ovpn-dashboard /etc/ovpn-dashboard

USER ovpndash
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/hub-entrypoint.sh"]
