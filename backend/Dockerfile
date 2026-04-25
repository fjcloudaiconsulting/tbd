FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc default-libmysqlclient-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x /app/entrypoint.sh

RUN addgroup --system --gid 1001 backend && \
    adduser --system --uid 1001 backend
USER backend

ENV PYTHONPATH=/app

EXPOSE 8000

# entrypoint.sh runs `alembic upgrade head` before exec'ing the CMD. This
# makes migrations a property of the image itself rather than the orchestrator
# spec — so PR migrations always run on the next deploy, even if .do/app.yaml
# was never re-synced and even if APP_ENV=production.
ENTRYPOINT ["/app/entrypoint.sh"]

# --proxy-headers makes uvicorn parse X-Forwarded-For / X-Forwarded-Proto
# so request.client.host becomes the real client IP (slowapi.get_remote_
# address feeds off it — without this the rate limit is effectively global
# per proxy IP).
#
# The trust list is restricted to RFC 1918 private ranges + loopback, not
# "*". Why it matters: the app is deployed on DO App Platform where the
# backend is a regular "service" with http_port. DO's ingress routes
# /api to it, but a public service can in principle receive direct
# internet-sourced connections on its own URL. If we trusted every source
# IP, uvicorn would return the LEFTMOST X-Forwarded-For entry (per
# ProxyHeadersMiddleware._TrustedHosts.get_trusted_client_host when
# always_trust=True) and any caller could forge that header and defeat
# the rate limiter. With the private-CIDR trust list uvicorn walks the
# header chain right-to-left and returns the first UNTRUSTED (public)
# host — which is the real client IP, because the actual proxies (nginx
# in dev, DO's edge in prod) terminate TCP on private IPs and append
# the real public client IP to the header. Forged leftmost entries are
# bypassed by the walk.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7,127.0.0.1,::1"]
