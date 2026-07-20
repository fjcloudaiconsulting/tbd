# Mailgun delivery webhooks (record-only) — design

Date: 2026-07-20
Status: APPROVED-WITH-RULINGS (architect, 2026-07-20), pending implementation

Feature B of three broadcast follow-ups (B webhooks → A admin UI → C API keys).

## Motivation

The shipped broadcast feature (#553/#554/#555) records a recipient as `sent` =
"accepted by Mailgun for delivery" (batch-send Ruling R1). That is all a batch
API call can tell us; true delivered / bounced / spam-complaint status only
arrives asynchronously via Mailgun webhooks. This feature ingests those events
and records the real per-recipient outcome.

**Record-only.** No suppression, no send-behaviour change, no unsubscribe. Those
stay gated behind the separate segment/unsubscribe follow-up (Ruling 10). This
feature only writes `delivery_status` and exposes it for reading.

## Architecture at a glance

Mailgun POSTs one signed event per request to a new public, signature-verified
endpoint. The handler verifies the HMAC, maps the event to a delivery outcome,
correlates it to the exact recipient row via a `v:broadcast_id` tag we now set
at send time plus the event's recipient email, and applies the outcome under a
sticky precedence rule so out-of-order/duplicate redelivery is safe. Delivery
counts are derived on read, never stored.

## W1 — Signature verification (exact contract)

- Inputs come from the JSON body's `signature` object `{timestamp, token,
  signature}` (NOT headers).
- `expected = hmac.new(key=settings.mailgun_webhook_signing_key.encode(),
  msg=(timestamp + token).encode(), digestmod=hashlib.sha256).hexdigest()`,
  compared to the provided `signature` with `hmac.compare_digest` (constant
  time).
- The signing key is the domain's **HTTP webhook signing key**, a NEW secret
  `mailgun_webhook_signing_key`, distinct from `mailgun_api_key`. Never logged.
- **Key UNSET ⇒ FAIL CLOSED.** When the key is empty the handler rejects every
  payload (404, "feature not enabled") and never reaches processing. There must
  be NO path where an empty key disables verification and accepts events. (This
  deliberately inverts `send_email`'s empty-`mailgun_api_key` dev no-op — an
  inbound security endpoint must fail closed.)
- Freshness: reject if `abs(now - int(timestamp)) > tolerance`, default 900 s
  via new `mailgun_webhook_timestamp_tolerance_s: int = 900`. Reject future
  timestamps beyond the same skew.

## W2 — HTTP status discipline

- Bad / missing signature ⇒ **401**. Key unset ⇒ **404**. Never 2xx for
  something we could not authenticate (a genuine key-misconfig should surface
  via Mailgun retries, not be silently swallowed; forged floods are handled by
  W3 regardless of status).
- Malformed JSON / missing `signature` fields ⇒ **400**.
- **Verified-but-unprocessable** (unknown event type, unparseable/absent
  `v:broadcast_id`, no matching recipient, ignored event) ⇒ **200 and drop**.
  This is the ONLY 2xx-drop path and it is gated behind a valid signature —
  mirrors the CSP endpoint's "never make an upstream retry what a retry can't
  fix." No body, no PII on any response.

## W3 — DoS ordering + caps (public route)

Check cheap→expensive, DB last:
1. `content-length` precheck → drop oversized before buffering (mirror
   `security.py`'s CSP handler).
2. Read body capped at `_MAX_BODY_BYTES = 256 KiB` (event-data carries full
   message headers; 16 KiB would truncate legit events).
3. JSON parse (bounded).
4. Extract `signature` → **HMAC verify** (W1). Everything below is gated on this.
5. Timestamp freshness (W1) → replay token (mandatory-additions).
6. Extract `event-data`, map event → status (W5), parse `v:broadcast_id`.
7. **DB lookup + conditional update LAST.**
- Rate limit `@limiter.limit("300/minute")` keyed on `get_client_ip` — generous,
  because in prod the peer is Mailgun's small egress set and a large send
  legitimately bursts `delivered` events. Signature+size are the primary abuse
  gate, not the limiter.

## W4 — MANDATORY send-side change: emit `v:broadcast_id`

Add `"v:broadcast_id": str(broadcast_id)` to the `data=` dict in
`email_service.send_batch`; thread `broadcast_id` through the `send_batch`
signature and the drain call site in `broadcast_service`. Message-level `v:`
variables ARE echoed to every recipient's events under
`event-data.user-variables` as **strings** (confirmed); **recipient-variables
are NOT echoed** (confirmed), so `v:broadcast_id` is the correct and only
correlation carrier. Parse back with `int(...)`, drop (200) on failure. The
dry-run path (`send_email`, single recipient, no recipient row) does NOT get
this tag and records no webhook status.

## W5 — Event → delivery_status mapping

Modern Mailgun webhooks POST ONE event per request (not a batched array).
- `delivered` → `delivered`
- `failed` + `event-data.severity == "permanent"` → `bounced_permanent`
- `failed` + `severity == "temporary"` → `bounced_temporary`
- `failed` + missing/unknown severity → `bounced_permanent` (surface as
  actionable; log the raw severity)
- `complained` → `complained`
- any other event (opened/clicked/unsubscribed/unknown) → 200-drop, no write.

## W6 — Idempotency & event ordering (correctness core)

Duplicates and out-of-order redelivery are the norm. Correctness rests on a
**sticky precedence lattice**, not last-writer-wins and not on the replay cache:

```
rank:  complained (4) > bounced_permanent (3) > delivered (2) > bounced_temporary (1) > NULL (0)
```

Apply the new status only if `rank(new) > rank(current)`. Terminal-negative
outcomes are sticky against a late/duplicate `delivered`, while a real
`delivered` still overrides an earlier soft bounce (temporary→delivered).
Worked: delivered then late complained → complained; complained then late
delivered → complained stays; bounced_temporary then delivered → delivered;
bounced_permanent then stray delivered → permanent stays.

- Implement as **read-modify-write in ONE committed transaction**: `SELECT ...
  FOR UPDATE` the recipient row, compare rank in Python, conditional `UPDATE`.
  Single-instance + tiny volume make row-lock contention a non-issue and it
  stays SQLite-CI-portable (no inline rank CASE).
- `delivery_updated_at` = the `event-data.timestamp` (event occurrence time),
  distinct from the W1 signature timestamp. Equal-rank duplicates are no-ops.

## W7 — Correlation robustness

Join on `(broadcast_id, lower(email))` against the snapshot `email` column
(case-insensitive; Mailgun tends to lowercase recipients). The email snapshot
survives `user_id` being nulled on user-delete, so it is the correct key. No
matching row (unknown/foreign broadcast_id, address mismatch) ⇒ log breadcrumb
+ 200-drop. Add `Index("ix_broadcast_recipient_email", "broadcast_id", "email")`
in the same migration to serve this lookup. >1 match (shouldn't happen — user
email is unique) ⇒ update all + log the anomaly.

## W8 — Data model (String columns, not enum)

Two NEW NULLABLE columns on `email_broadcast_recipients`:
- `delivery_status` `VARCHAR(32)` NULL — values `delivered` /
  `bounced_permanent` / `bounced_temporary` / `complained`, an app-level
  `frozenset` + rank map validated in code (same discipline as `segment`,
  Ruling 4 — NOT a DB enum, avoids the MySQL enum-ALTER landmine).
- `delivery_updated_at` `DATETIME` NULL.

Additive migration: the two nullable columns + the W7 index. All-nullable ⇒ no
backfill. **MySQL-verify on real MySQL 8** (nullable add is `ALGORITHM=INSTANT`)
— this is the ABN-`.TAB` native-ENUM landmine class; do not trust SQLite CI
alone. No `ALTER ENUM`.

## W9 — Counts exposure: compute-on-read

Do NOT add stored count columns to `email_broadcasts` and do NOT have the
webhook bump broadcast-row counters (reintroduces the write-contention/drift the
codebase avoids). Derive at response time from `GROUP BY delivery_status` over
recipient rows, mirroring `_recompute_broadcast_counters`. Expose four derived
fields on `BroadcastResponse`: `delivered_count`, `bounced_count` (permanent),
`soft_bounced_count` (temporary), `complained_count`. The list endpoint uses ONE
`GROUP BY broadcast_id, delivery_status`, not N queries.

`GET /api/v1/admin/broadcasts/{id}/recipients` (superadmin, paginated) exposes
per-recipient rows incl. `delivery_status` so the operator can see WHICH
addresses bounced and clean up dead accounts.

## W10 — Audit / PII

- **No `audit_events` row per webhook event** — the anonymous, high-volume
  stream would dilute the admin-failure signal (same reasoning as the CSP sink).
  structlog breadcrumbs only.
- **Never log the raw recipient email** (Ruling 13 / MA5 hold): log
  `broadcast_id`, event, severity, masked/omitted address. The webhook adds no
  new PII sink; the address already lives on the recipient row.
- `GET /{id}/recipients` may show bounced addresses because it is
  `require_superadmin`-gated and operationally necessary. Paginate it.

## W11 — Handler shape

- SYNCHRONOUS in-handler (async enqueue is overkill for ~1 batch of dribbled
  events).
- New `backend/app/routers/webhooks.py`, `APIRouter(prefix="/api/v1/webhooks")`,
  route `POST /mailgun`, `include_in_schema=False`, **ZERO auth dependency** —
  use `get_session_factory` + its own session like `csp_report`, NOT
  `get_db`+auth. Register in `main.py`. Do NOT add `get_current_user` /
  `require_superadmin` (would break the public contract).

## Config + operator runbook (mandatory additions)

- New config in `config.py`: `mailgun_webhook_signing_key: str = ""` and
  `mailgun_webhook_timestamp_tolerance_s: int = 900`, documented by the existing
  `broadcast_*` block.
- Replay-token helper in `redis_client.py`: `SET
  webhook:mailgun:token:{token} 1 NX EX <tolerance+margin>`; **fail-OPEN** on
  Redis-unavailable (DoS hygiene, not the security boundary — signature
  verification is, and needs no Redis). Do NOT reuse the auth `require_client`
  fail-closed pattern here.
- Operator one-time: register the webhook URL in Mailgun for `delivered`,
  `permanent_fail`, `temporary_fail`, `complained` ONLY; set the domain HTTP
  webhook signing key in DO secrets + `.do/app.yaml`; confirm the webhook is on
  the SAME sending domain broadcasts use; ensure `email_from` Reply-To is a
  monitored mailbox.

## Testing (required matrix)

valid / invalid / missing signature; unset key → 404; oversized body drop;
out-of-order (`complained` then late `delivered` stays complained);
`bounced_temporary` then `delivered` → delivered; duplicate event no-op; unknown
`broadcast_id` → 200-drop; unmatched email → 200-drop; `failed` + missing
severity → `bounced_permanent`; ignored event type → 200-drop; case-insensitive
email match; `v:broadcast_id` round-trips as a string; the migration is
MySQL-verified (manual gate). Mock Mailgun; no real HTTP.

## Out of scope (confirmed record-only)

No suppression, no send-behaviour change, no unsubscribe — gated behind the
segment/unsubscribe follow-up before any non-transactional audience. This
feature only writes `delivery_status` and reads it back.

## Rollout

One PR: migration + model columns/index, config, `send_batch` `v:broadcast_id`
(W4), the webhooks router + signature/replay/mapping/precedence logic, the
`BroadcastResponse` derived counts + `GET /{id}/recipients`, tests. Migration is
a MySQL merge gate. On merge + deploy, the operator registers the Mailgun
webhook + signing key; until then the endpoint fails closed (404) and no
delivery status is recorded (send behaviour unchanged).
