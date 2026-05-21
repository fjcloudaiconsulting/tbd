---
name: uvicorn.error logger name (cosmetic)
description: P3 polish — uvicorn's "uvicorn.error" logger name is misleading because it carries info/warning/error events alike. Cosmetic only; no behavior change.
type: project
originSessionId: 672f9f52-7ffa-43a4-9e88-ffc9e47e0229
---
## What

Backend JSON logs surface lines like:

```json
{"event": "Application shutdown complete.", "level": "info", "logger": "uvicorn.error", "timestamp": "..."}
```

The `logger` field reads `uvicorn.error` even for INFO events. This is correct Python-logging behavior: uvicorn defines exactly two named loggers (`uvicorn.access` for HTTP access logs, `uvicorn.error` for everything else — startup, shutdown, app lifecycle, AND real errors). The `level` field next to it is the actual severity.

**Not a bug.** Just an awkward inherited convention from uvicorn upstream.

## Why it's worth noting

Operators reading logs at a glance might skim `"logger": "uvicorn.error"` and assume something failed. With prod log routing (Datadog, Loki, Grafana), the level is usually the prominent column and `logger` is dim metadata, so most operators tune it out. But for support and debugging, it's a small papercut.

## How to fix (when it's worth doing)

Two options, both ~5-line changes:

1. **Structlog processor** in `backend/app/logging.py` that rewrites `logger == "uvicorn.error"` → `"uvicorn"` (or `"uvicorn.lifecycle"`) in the JSON renderer. Lowest blast radius.

2. **Configure uvicorn's `LOGGING_CONFIG`** to rename the logger at the source. More invasive (touches uvicorn config) but cleaner.

## Priority

P3 — polish, no behavior impact. Surface in any future "operational hygiene" pass; not launch-blocking.

## Captured

2026-05-02 during L3.10 implementation. User flagged the question while I was doing pre-merge fixes; explicitly tagged as low priority. No action taken on this branch.
