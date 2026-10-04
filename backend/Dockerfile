# Multi-stage (INFRA-39). Targets:
#   migrations  one-shot job image: `python scripts/migrate.py` (alembic upgrade head)
#   prod        the serving image (uvicorn). LAST stage on purpose: a plain
#               `docker build` (DO App Platform, docker compose) builds it.
# Both share the `runtime` stage; they differ only in CMD. Dependencies are
# installed (uv, from uv.lock) into a venv in `builder` (all wheels, no compiler needed); only the
# venv reaches either image.
#   docker build --target migrations -t tbd-migrations backend
#   docker build --target prod       -t tbd-backend    backend
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /uvx /bin/
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

ARG INSTALL_DEV=false

# Plain COPY, no BuildKit mounts: DO App Platform's builder is not assumed to support them.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project --no-default-groups --group app \
        $([ "$INSTALL_DEV" = "true" ] && echo "--group dev")

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

# Baked by the shared build-image workflow (RELEASE_CONTRACT.md section 3) and
# reported by /health. Last in this stage so the layers above stay cached
# across commits. A plain build (DO App Platform) gets "dev".
ARG APP_VERSION=dev
ARG APP_REVISION=dev
ENV TBD_APP_VERSION=$APP_VERSION \
    TBD_APP_REVISION=$APP_REVISION

FROM runtime AS migrations

# Run as a true init step (the `migrate` init container in production, see
# aws-infra clusters/platform/tbd-prod/backend.yaml), NOT in the serving process: `alembic upgrade head` in the entrypoint
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
