# Admin email broadcast (MVP) — design

Date: 2026-07-18
Status: approved, pending implementation

## Motivation

A DNS change on 2026-07-17 made the app unreachable for a stretch. We want to
email existing customers a short apology and an all-clear now that the app is
verified back up (`/health` ok, `/ready` database connected, login API 422).

There is no existing way to email a segment of users. Rather than a throwaway
script, we build a small, reusable, superadmin-only **email broadcast**
capability. The outage apology is its first payload; future incident notices or
service announcements reuse it.

This is a deliberately tight MVP. The apology should go out today.

## Scope

**In:**
- One new backend feature: compose a broadcast, dry-run it, send it one-by-one
  to a fixed audience segment, track per-recipient delivery, resume after a
  restart, audit every action.
- Audience segment: **active + verified users** (`is_active=1 AND
  email_verified=1`). This is the real customer base and avoids emailing
  unverified/bot/dead addresses that would harm sender reputation.
- Personalization: `Hi {first_name},` with a `Hi there,` fallback when
  `first_name` is null.
- Footer: a single account-context line, no unsubscribe (an outage/service
  notice is transactional; everyone should see it).

**Deferred (explicit non-goals for this PR):**
- Admin UI. Ships as a **same-day follow-up PR**; the apology is triggered today
  via authenticated API calls. See "Rollout".
- Scheduled send-later, saved templates, a segment/filter builder, open/click
  analytics, and an unsubscribe-preference surface. Each is a later increment.

## Audience

`segment` is an enum column so the model can grow, but the MVP ships exactly one
value: `active_verified`. Resolution query:

```sql
SELECT id, email, first_name
FROM users
WHERE is_active = 1 AND email_verified = 1
```

Cross-org by design: a platform-level broadcast reaches users across every org.
This is a superadmin action, not an org-scoped one.

## Data model

Two new tables. Integer PKs, MySQL enums via
`values_callable=lambda x: [e.value for e in x]` (project convention).

### `email_broadcasts`
- `id` PK
- `subject` `String(200)`, not null
- `body_template` `Text`, not null — the authored body, may contain the literal
  token `{first_name}`. Stored raw; rendered per recipient at send time.
- `segment` `Enum(BroadcastSegment)` — `active_verified` only for now
- `status` `Enum(BroadcastStatus)` — `draft` | `sending` | `completed` |
  `failed`
- `created_by_user_id` FK → users, `ON DELETE SET NULL` (mirrors announcements)
- `total_recipients` `Integer`, nullable (populated at send-materialization)
- `sent_count` / `failed_count` / `skipped_count` `Integer`, default 0
- `dry_run_sent_at` `DateTime`, nullable — gate for send
- `confirmed_at` `DateTime`, nullable
- `created_at` / `started_at` / `completed_at` timestamps

Body is stored as a template, not pre-rendered, because rendering is
per-recipient (the `{first_name}` substitution differs per row). The plain-text
and HTML shells are applied at send time by a single renderer so the stored
content stays canonical.

### `email_broadcast_recipients`
- `id` PK
- `broadcast_id` FK → email_broadcasts, `ON DELETE CASCADE`
- `user_id` FK → users, `ON DELETE SET NULL`
- `email` `String(320)`, not null — **snapshot** of the address at
  materialization time, so we know exactly who we targeted even if the user
  later changes email or is deleted
- `first_name` `String(100)`, nullable — snapshot for rendering
- `status` `Enum(RecipientStatus)` — `pending` | `sent` | `failed` | `skipped`
- `error` `Text`, nullable
- `attempts` `Integer`, default 0
- `sent_at` `DateTime`, nullable
- Unique constraint `(broadcast_id, user_id)` — the idempotency guarantee. The
  drain only ever sends to `pending` rows, so a resume or re-trigger cannot
  double-send.

## API

Router `/api/v1/admin/broadcasts`, every endpoint gated by the same
`require_superadmin` dependency as `admin_announcements` (403 if
`is_superadmin` is false). Every write emits an audit event via
`audit_service.record_audit_event` with `event_type` `broadcast.*`,
`target_org_id=None` (platform-level), `ip_address=get_client_ip(request)`
(never the raw peer — see reference_audit_client_ip_single_helper).

- `POST /` — create a draft from `{subject, body_template, segment}`. Response
  includes a live `recipient_count` preview for the segment (a `COUNT(*)`, no
  rows materialized yet). Audit `broadcast.create`.
- `GET /{id}` — draft/status + counts, for progress polling.
- `GET /` — list broadcasts (history).
- `GET /{id}/preview` — the rendered HTML + text for a sample first name (and
  the fallback), for eyeballing before sending. No side effects.
- `POST /{id}/dry-run` — render and send the email to the **calling
  superadmin's own address only**. Stamps `dry_run_sent_at`. Audit
  `broadcast.dry_run`. This is the mandatory pre-send check.
- `POST /{id}/send` — the guarded trigger. Precondition failures return 422
  with a machine code; a wrong lifecycle state returns 409. All must hold:
  1. status is `draft` (else 409 `broadcast_not_draft`),
  2. `dry_run_sent_at` is set (else 422 `dry_run_required`),
  3. request body `confirm_subject` matches the broadcast subject exactly (else
     422 `confirm_subject_mismatch`),
  4. request body `confirm_recipient_count` matches the **freshly recomputed**
     segment count exactly (else 422 `confirm_count_mismatch`),
  5. the recomputed count is within `BROADCAST_MAX_RECIPIENTS` (else 422
     `recipient_cap_exceeded`).
  On pass, in one transaction: recompute the segment, materialize a `pending`
  recipient row per user (snapshotting email + first_name), set
  `total_recipients`, flip status `draft → sending`, stamp `confirmed_at`, and
  launch the background drain. A call while already `sending`/`completed` gets
  409 `broadcast_not_draft`. Audit `broadcast.send`.
- `POST /{id}/resume` — re-launch the drain for any `pending` rows left by a
  restart or partial run. Idempotent. Audit `broadcast.resume`.

The `confirm_recipient_count` echo is the load-bearing safety rail: if a segment
query bug (or organic growth) made the audience larger than the operator
expects, the typed number will not match the recomputed count and the send is
refused. Count is recomputed inside the send transaction so it cannot drift
between confirmation and materialization.

A hard backstop cap `BROADCAST_MAX_RECIPIENTS` (env, default 10000) makes `send`
refuse a segment larger than the cap outright, guarding against a query bug that
would blast far more people than the product could plausibly have. Raising it is
a deliberate env change.

## Send execution

A background task (launched with the app's existing session_factory, not the
request session) drains the broadcast:

1. Select `pending` recipients for the broadcast, ordered by `id`.
2. For each: re-check the user is **still** active + verified. If not, mark
   `skipped` (they deactivated/unverified between materialization and send) and
   continue.
3. Render `Hi {first_name},` / `Hi there,` into the body template, wrap in the
   shared HTML + text shell, call the existing
   `email_service.send_email(to=snapshot_email, subject, body_html, body_text)`.
4. Mark the row `sent` or `failed` (with `error`), bump `attempts` and the
   broadcast's counters.
5. Sleep `BROADCAST_PACING_SECONDS` (env, default 1.0) between sends to avoid
   spam-filter tripping.

One recipient's failure never halts the batch (each send is try/excepted). When
no `pending` rows remain, set `status = completed` (even if some `failed` —
`failed_count` records them; a `resume` can retry). Real sends happen only in
prod; in dev `send_email` logs `email_sent_dev` and returns True, so the whole
flow is exercisable locally without touching Mailgun.

Concurrency guard: the `draft → sending` transition is atomic (a conditional
UPDATE on status), so two concurrent `send`/`resume` calls cannot both start a
drain. A recipient row is only ever advanced out of `pending` by the single
active drain.

Restart behavior: a crash mid-drain leaves rows `pending` and the broadcast
`sending`. `POST /{id}/resume` continues from there. (Automatic
scheduler-driven resume is the deferred hardening path; not in this MVP.)

## Email content — broadcast #1 (the apology)

Subject:

> The Better Decision is back up, and I'm sorry for the downtime

Body template:

> Hi {first_name},
>
> Yesterday a DNS change on our side made The Better Decision unreachable for a
> while. That was our mistake, and I'm sorry for the disruption.
>
> It is fully resolved. The app is back up and running normally, and your
> account and data were never at risk. This was a connectivity problem, so
> nothing in your account was touched or lost.
>
> Thank you for your patience, and for trusting us with something as personal as
> your money. If anything still looks off to you, just reply to this email and I
> will look into it right away.
>
> Warmly,
> Flamarion
> The Better Decision

Footer (appended by the shell, not part of the authored body):

> You're receiving this because you have a The Better Decision account.

No em-dashes in customer copy (project convention). The "data was never at risk"
line is accurate for a DNS/availability incident; it is contingent on the
operator confirming the outage was connectivity-only with no data impact.

## Safety rails (summary)

Real customer email is irreversible, so the rails are the feature:
1. Superadmin-only, every action audited.
2. Mandatory dry-run to self before any real send.
3. Typed confirm echoing subject **and** recipient count.
4. Segment count recomputed inside the send transaction (no drift).
5. Per-recipient idempotency via the unique constraint; drain touches only
   `pending`.
6. Segment re-check at send time (skip users who lapsed).
7. Pacing between sends.
8. Hard recipient cap backstop.
9. Prod-only real send; dev logs.

## Testing

- Model/migration: tables create; unique constraint rejects a duplicate
  `(broadcast_id, user_id)`.
- Segment resolution: only active+verified users counted/materialized; inactive,
  unverified excluded.
- Auth: every endpoint 403 for a non-superadmin.
- `send` gating: 422 without a dry-run; 422 on subject mismatch; 422 on count
  mismatch; 409 when not `draft`; cap exceeded refused.
- Idempotency: a second `send`/`resume` never re-sends a `sent` recipient
  (assert `send_email` call count equals distinct pending recipients).
- Drain: renders first-name and fallback correctly; a mid-list `send_email`
  failure marks that row `failed` and the drain still completes the rest;
  `skipped` for a user who lapsed post-materialization.
- Audit: each write emits the expected `broadcast.*` event with
  `target_org_id=None` and a resolved (non-raw) ip.
- `send_email` is mocked throughout; no real Mailgun calls in tests.

## Rollout

1. **PR 1 — backend** (this spec): model + migration, router, send engine, safety
   rails, audit, tests. On merge + deploy, trigger broadcast #1 today via
   authenticated superadmin API calls: `POST /` (create), `POST /{id}/dry-run`
   (verify it lands in your inbox), then `POST /{id}/send` with the typed
   subject + count confirm. I hand over the exact commands.
2. **PR 2 — minimal admin UI** (same-day follow-up): a superadmin
   `/admin/broadcasts` page to compose, preview, send-test, confirm-and-send,
   and watch live progress. No new backend.

Each PR follows the normal flow: dispatched agent review with findings folded
before the operator merges; `main` needs the operator's human approval.

## Follow-ups / future increments

- Scheduler-driven automatic resume (restart resilience without the manual
  button).
- Saved templates, scheduled send-later, richer segment builder.
- Unsubscribe-preference surface (required before any non-transactional /
  marketing broadcast).
- Open/click analytics.
