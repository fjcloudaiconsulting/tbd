FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc default-libmysqlclient-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN addgroup --system --gid 1001 backend && \
    adduser --system --uid 1001 backend
USER backend

ENV PYTHONPATH=/app

EXPOSE 8000

# --proxy-headers + --forwarded-allow-ips="*" makes uvicorn parse the
# X-Forwarded-For / X-Forwarded-Proto headers set by the upstream proxy
# (nginx in dev, DO App Platform LB in prod) so request.client.host is
# the real client IP — not the LB egress IP. slowapi.get_remote_address
# feeds off request.client.host, so without these flags the rate limit
# is effectively global (one LB IP shared across all callers). The
# backend port is only reachable from inside the container network in
# every target environment, so trusting proxy headers from any source
# IP is safe.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
