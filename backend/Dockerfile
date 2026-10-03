# Multi-stage (INFRA-39). Targets:
#   migrations  one-shot job image: `python scripts/migrate.py` (alembic upgrade head)
#   prod        the serving image (uvicorn). LAST stage on purpose: a plain
#               `docker build` (DO App Platform, docker compose) builds it.
# Both share the `runtime` stage; they differ only in CMD. The build toolchain
# (gcc, headers, pip cache) lives in `builder` and never reaches either.
#   docker build --target migrations -t tbd-migrations backend
#   docker build --target prod       -t tbd-backend    backend
FROM python:3.12-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc default-libmysqlclient-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*

ARG INSTALL_DEV=false

RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt && \
    if [ "$INSTALL_DEV" = "true" ]; then \
        pip install --no-cache-dir -r requirements-dev.txt; \
    fi

FROM python:3.12-slim AS runtime

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY . .

RUN addgroup --system --gid 1001 backend && \
    adduser --system --uid 1001 backend
USER backend

ENV PATH=/opt/venv/bin:$PATH
ENV PYTHONPATH=/app
# Force unbuffered stdout/stderr so log lines reach the docker log driver
# (and the App Platform log capture) immediately. Without this, Python
# block-buffers stdout when not attached to a TTY, which makes
# `./tbd logs -f` and the prod log stream appear to "stop" while the
# buffer fills before flushing.
ENV PYTHONUNBUFFERED=1

FROM runtime AS migrations

# Run as a true init step (App Platform PRE_DEPLOY job in .do/app.yaml, a k8s
# Job), NOT in the serving process: `alembic upgrade head` in the entrypoint
# would gate uvicorn's port-bind on migration time, so a slow migration looks
# like a hung container and is rolled back.
CMD ["python", "/app/scripts/migrate.py"]

FROM runtime AS prod

EXPOSE 8000

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
