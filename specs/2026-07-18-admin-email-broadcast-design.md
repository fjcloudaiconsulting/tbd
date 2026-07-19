# Admin email broadcast (MVP) — design

Date: 2026-07-18
Status: approved-with-rulings (architect, 2026-07-18), pending implementation

> **Architect rulings are LOCKED at the bottom of this doc and OVERRIDE any
> conflicting text above.** Read "Architect rulings" before implementing. The
> body sections below have been amended to match; the ruling list is the
> authoritative source if anything diverges.

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

`segment` is a validated `String` column (Ruling 4) so the model can grow
ALTER-free, but the MVP ships and validates exactly one value: `active_verified`.
Resolution query:

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
- `segment` **`String(32)`** with app-level validation (Ruling 4) — NOT a DB
  enum. This is the "designed to grow" axis; a validated string keeps future
  values ALTER-free and dodges the MySQL-ENUM-ALTER landmine. v1 validation
  accepts exactly `active_verified` (Ruling 10).
- `status` `Enum(BroadcastStatus, name="broadcast_status", values_callable=...)`
  — `draft` | `sending` | `completed` | `failed` (closed set → native enum, per
  `announcement.py`)
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
- `status` `Enum(RecipientStatus, name="broadcast_recipient_status",
  values_callable=...)` — `pending` | `sent` | `failed` | `skipped` (closed set)
- `error` `Text`, nullable
- `attempts` `Integer`, default 0
- `sent_at` `DateTime`, nullable
- Unique constraint `(broadcast_id, user_id)` — dedupes materialization INSERTs.
- Index `(broadcast_id, status)` (Ruling 6) — serves the drain's
  `WHERE broadcast_id=? AND status='pending'` select.

Idempotency is NOT the unique constraint alone (it only stops duplicate
INSERTs). The no-double-send guarantee is the per-row atomic claim in the drain
(Ruling 3): each recipient is advanced with
`UPDATE ... SET status='sent'/'failed' WHERE id=:rid AND status='pending'` and
the send proceeds only if that claim's `rowcount == 1`.

## API

Router `/api/v1/admin/broadcasts`, every endpoint gated by the same
`require_superadmin` dependency as `admin_announcements` (403 if
`is_superadmin` is false). Every write emits an audit event via
`audit_service.record_audit_event` with `event_type` `broadcast.*`,
`target_org_id=None` (platform-level, Ruling 8), `ip_address=get_client_ip(request)`
(never the raw peer — see reference_audit_client_ip_single_helper, #552 guard).

Audit `detail` carries NO recipient PII (Ruling 13): only `broadcast_id`,
`segment`, counts (`total_recipients`/`sent`/`failed`/`skipped`), and at most the
subject — never recipient email addresses, never the rendered body. Mirrors the
`_audit_detail` announcement pattern (which logs `title_length`, not the title).

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

The drain is a **tracked** asyncio task, NOT a bare `create_task` (Ruling 1):
- Launched only AFTER the materialization transaction commits (Ruling 2), so the
  drain's independent session can see the `pending` rows.
- Held by a strong reference in a module-level `set[asyncio.Task]` (or
  `app.state`) so the GC cannot collect it mid-flight.
- `task.add_done_callback(...)` logs any exception via structlog and discards
  the task from the set — failures are observed, never silently swallowed.
- Runs on its own session from the injected `session_factory` (Ruling 2), never
  the request session.
- The whole drain body is wrapped so an unhandled error sets the broadcast
  `status` sensibly and logs, rather than dying silently.

In-process active-drain registry (Ruling 3): a module-level `set[int]` of
broadcast ids with a live drain. A `send`/`resume` for an id already draining is
an idempotent no-op / 409. Safe because `instance_count: 1`.

Drain loop, per `pending` recipient (ordered by `id`):
1. Re-check the user is **still** active + verified (Ruling 9). If not,
   atomically claim → `skipped` and continue.
2. Render the greeting: `Hi {first_name},` / `Hi there,`. `first_name` is
   user-controlled, so **`html.escape(first_name)`** before HTML interpolation
   (Ruling 11); the plain-text part stays raw. Substitute with
   `body.replace("{first_name}", value)`, **never `str.format()`** (Ruling 11),
   so stray braces in operator copy can't raise `KeyError` or open a
   format-string vector. Wrap in the shared HTML + text shell.
3. **Atomic claim** (Ruling 3):
   `UPDATE ... WHERE id=:rid AND status='pending'`; proceed only if
   `rowcount == 1` (else another drain already took it — skip).
4. `email_service.send_email(to=snapshot_email, subject, body_html, body_text)`.
   Key off its **return bool** (Ruling 12): falsy → mark `failed` with `error`;
   still `try/except` defensively. Bump `attempts` and the broadcast counters.
5. Sleep `BROADCAST_PACING_SECONDS` (env, default 1.0) between sends.

One recipient's failure never halts the batch. When no `pending` rows remain,
set `status = completed` (even with some `failed`). Real sends happen only in
prod; in dev `send_email` logs `email_sent_dev` and returns True, so the whole
flow is exercisable locally without touching Mailgun. Note: a prod `send_email`
True means "Mailgun accepted for queued delivery", NOT delivered.

Concurrency guard (Ruling 3): the `draft → sending` transition is
`UPDATE email_broadcasts SET status='sending', started_at=NOW()
WHERE id=:id AND status='draft'` with a `rowcount == 1` check (else 409
`broadcast_not_draft`). Combined with the per-row atomic claim and the
in-process registry, a double-send is impossible even if the in-process guard is
ever bypassed by a future multi-instance change.

Restart behavior: a crash mid-drain leaves rows `pending` and the broadcast
`sending`. `POST /{id}/resume` continues, retrying only rows with
`attempts < BROADCAST_MAX_ATTEMPTS` (env, default 3, Ruling 12) so a permanently
bad recipient is not hammered on every resume. (Automatic scheduler-driven
resume and Mailgun bounce/complaint webhooks are deferred follow-ups.)

## Email content — broadcast #1 (the apology)

Subject:

> The Better Decision is back up, and I'm sorry for the downtime

Body template:

> Hi {first_name},
>
> On Friday, July 17, a DNS change on our side made The Better Decision
> unreachable. The outage lasted more than 12 hours, and it was fully resolved
> in the early hours of Saturday, July 18. That was our mistake, and I'm sorry
> for the disruption and for how long it took to fix.
>
> Your account and your data were never at risk. This was a connectivity
> problem, so nothing in your account was touched or lost.
>
> Everything is back to normal now. If anything still looks off to you, just
> reply to this email and I will look into it right away.
>
> Thank you for your patience, and for trusting us with something as personal as
> your money.
>
> Warmly,
> The Better Decision

Footer (appended by the shell, not part of the authored body):

> You're receiving this because you have a The Better Decision account.

No em-dashes in customer copy (project convention). The "data was never at risk"
line is accurate for a DNS/availability incident; it is contingent on the
operator confirming the outage was connectivity-only with no data impact.

Compliance boundary lock (Ruling 10): no-unsubscribe is acceptable for THIS
transactional outage notice only. Any broadcast that (a) adds a new `segment`
value, (b) carries promotional / re-engagement / marketing content, or (c) is
recurring MUST ship an unsubscribe + suppression mechanism first. Enforced by
keeping `active_verified` the ONLY accepted `segment` value in v1 validation, so
a marketing send cannot reuse this path without new code passing review.

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
  `target_org_id=None` and a resolved (non-raw) ip; `detail` contains NO
  recipient email address (Ruling 13/14c).
- Concurrency (Ruling 14a): two `send` (or two `resume`) calls yield exactly one
  drain; assert `send_email` call count equals the distinct pending recipients.
- Attempts cap (Ruling 14b): a row at `BROADCAST_MAX_ATTEMPTS` is not retried by
  `resume`.
- HTML escape (Ruling 14d): a `first_name` containing `<`/`&` is escaped in the
  HTML body (raw in text).
- Drain-raises path (Ruling 14e): an unhandled drain error is observed via the
  done-callback (logged, task not silently dropped), and the broadcast status
  reflects it.
- `send_email` is mocked throughout; no real Mailgun calls in tests.

## Rollout

1. **PR 1 — backend** (this spec): model + migration, router, send engine, safety
   rails, audit, tests. **Migration is a merge gate on real MySQL** (Ruling 5):
   the reviewer runs it up/down on an isolated `-p team-*` MySQL stack and
   confirms the two `status` ENUM columns store lowercase values, because SQLite
   CI green does not prove MySQL enum DDL. New settings `BROADCAST_MAX_RECIPIENTS`
   (10000), `BROADCAST_PACING_SECONDS` (1.0), `BROADCAST_MAX_ATTEMPTS` (3) are
   declared pydantic-settings style in `config.py` (Ruling 15). On merge +
   deploy, trigger broadcast #1 today via
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

## Architect rulings (LOCKED 2026-07-18)

Verdict: **APPROVED-WITH-RULINGS.** Fold 1-14 verbatim; 15 is advisory. These
override any conflicting text above.

1. **Drain = tracked task with a done-callback, NOT a bare `create_task`; NOT
   the scheduler.** Strong ref in a module-level `set` (or `app.state`);
   `add_done_callback` logs exceptions + discards; own session from
   `session_factory`; whole body wrapped so an unhandled error sets status +
   logs. Manual `resume` accepted for MVP (per-recipient log makes loss
   recoverable).
2. **Launch the drain AFTER the materialization transaction commits; pass the
   app `session_factory` (via `get_session_factory`), not the request `db`** —
   else the drain's session can't see the uncommitted rows and no-ops.
3. **Status flag alone is insufficient — add a per-instance active-drain guard
   AND a per-row atomic claim.** (a) `send`: `UPDATE ... SET status='sending',
   started_at=NOW() WHERE id=:id AND status='draft'`, check `rowcount==1` else
   409. (b) In-process `set[int]` of draining ids → second `resume`/`send` is
   no-op/409 (safe on `instance_count:1`). (c) Each recipient claimed with
   `... WHERE id=:rid AND status='pending'`, send only if `rowcount==1`.
4. **`segment` → `String(32)` app-validated; `status` + `recipient.status` →
   native `Enum(values_callable=..., name=...)`.** Growth axis (`segment`) must
   not be a DB enum (ALTER-ENUM landmine: SQLite CI green, prod 500). Closed sets
   stay native enums for DB integrity + parity with `announcement.py`.
5. **Migration verified on real MySQL (isolated `-p team-*` stack), up/down,
   lowercase values stored — a merge gate, not a nicety.** SQLite green != MySQL
   green for enum DDL.
6. **Keep `UNIQUE(broadcast_id, user_id)`, `broadcast_id` CASCADE, `user_id` SET
   NULL; ADD index `(broadcast_id, status)`.** Snapshotting email/first_name is
   required. NULL-dedup caveat accepted (materialization is one-shot).
7. **Typed subject+count confirm rail and hard cap KEPT as-is** — do not weaken.
   Count recomputed inside the send transaction is load-bearing.
8. **Cross-org `target_org_id=None` in audit APPROVED** (matches
   `admin_announcements`). `ip_address=get_client_ip(request)`, never raw peer.
9. **Segment re-check at send time and `skipped` status KEPT** — cheap
   correctness.
10. **No-unsubscribe acceptable for THIS transactional notice only; boundary
    locked.** New segment value, promotional/re-engagement content, or recurring
    send → unsubscribe + suppression required first. Keep `active_verified` the
    only accepted segment in v1 validation.
11. **Escape user-controlled values before HTML interpolation; substitute with
    `.replace`, not `.format()`.** `html.escape(first_name)` for the HTML body
    (text stays raw). `_render_html` inserts paragraphs verbatim, so this is the
    real injection surface.
12. **Mailgun semantics: key off `send_email`'s return bool; add attempts cap.**
    Falsy return → `failed` (still try/except). `resume` retries only
    `attempts < BROADCAST_MAX_ATTEMPTS` (env, default 3). Bounce/complaint
    webhooks DEFERRED (named follow-up; `active_verified` already filters dead
    addresses).
13. **No recipient PII in audit `detail`** — `broadcast_id`, `segment`, counts,
    at most subject; never emails, never body. Mirror `_audit_detail`.
14. **Tests add:** (a) concurrency → one drain, `send_email` count == distinct
    pending; (b) attempts cap not retried; (c) audit detail has no email; (d)
    HTML-escape a `<`/`&` first_name; (e) drain-raise observed via done-callback.
    Ruling 5 MySQL check is a manual gate, not a CI test. `send_email` mocked
    throughout.
15. **(Advisory)** pacing×cap worst case ≈ 2.7h single-task runtime; a deploy
    kills it mid-drain (recoverable via `resume`). Non-issue at today's audience;
    operator confirms the recomputed count is small before the first send and
    does not raise the cap without accepting longer unbroken runtimes. Declare
    `BROADCAST_MAX_RECIPIENTS`, `BROADCAST_PACING_SECONDS`, `BROADCAST_MAX_ATTEMPTS`
    in `config.py`.

## Batch-sending revision (2026-07-19) — APPROVED-WITH-RULINGS (architect)

Operator direction: send via **Mailgun batch sending** (Mailgun best practices,
https://documentation.mailgun.com/docs/mailgun/user-manual/sending-messages/batch-sending),
not the per-recipient one-by-one loop shipped in #553. This revision OVERRIDES
the "Send execution" section and Rulings 1/11/12 where they conflict. #553 is
merged + deployed but has never sent (no UI, no broadcast created), so the send
core can be refactored freely; the tables/router/gating stay.

### New send model
- Resolve active+verified recipients, chunk into batches of `BROADCAST_BATCH_SIZE`
  (default 1000, hard-capped at Mailgun's 1000/call limit).
- Per batch: ONE Mailgun API call with `to` = the batch's addresses AND
  `recipient-variables` (a JSON map `{email: {..per-recipient vars..}}`). Mailgun
  sends an individualized message to each recipient and manages delivery cadence.
  **Recipient-variables are mandatory** — without them Mailgun exposes all
  addresses in the `to` header (privacy breach).
- `email_service` gains a `send_batch(to_list, subject, body_html, body_text,
  recipient_variables) -> bool` (single-recipient `send_email` stays for
  transactional mail). Dev mode logs batch size, sends nothing.
- Optional pacing `BROADCAST_PACING_SECONDS` now applies BETWEEN batches, not
  between recipients. For a sub-1000 audience this is a single call, no pacing.

### Personalization + escaping (Ruling 11 rework)
- Body uses Mailgun tokens: `%recipient.first_name_html%` in the HTML part,
  `%recipient.first_name_text%` in the text part. The operator still authors with
  `{first_name}`; at send we translate `{first_name}` → the HTML token in the
  HTML body and → the TEXT token in the text body.
- The operator's body content is `html.escape`-d ONCE (static, shared across the
  batch). Per recipient, the variables map carries
  `first_name_html = html.escape(first_name or "there")` and
  `first_name_text = first_name or "there"` (raw). Mailgun substitutes raw, so
  the HTML escaping must be pre-applied by us — the two-variable split is how we
  keep escaped-in-HTML + raw-in-text with Mailgun's single-value-per-key model.

### OPEN QUESTIONS FOR THE ARCHITECT
1. **Per-recipient status model.** A batch call returns ONE per-batch result
   ("queued"), not per-recipient outcomes; true per-recipient delivered/bounced/
   complained status only arrives via Mailgun webhooks/events (DEFERRED). So the
   DB can only record "accepted-for-delivery by Mailgun", not "delivered".
   Options: (A) repurpose the existing `sent` recipient status to mean "accepted
   by Mailgun" (UI labels it "Queued") — NO enum change, avoids the
   ALTER-ENUM-on-MySQL landmine; (B) add a `queued` enum value via a
   MySQL-verified `ALTER`. Recommend (A). Rule.
2. **Batch idempotency / crash safety.** Claim-before-send (mark the batch's rows
   `sent/queued` in one UPDATE, then call Mailgun, mark `failed` on API error)
   favors NEVER-double-send but risks a lost batch if we crash between claim and
   a successful Mailgun receipt. Claim-after-send favors never-lost but risks
   double-send on crash. For an apology, which way? (Batches are ≤1000 and this
   audience is ~1 batch, so the window is tiny either way.) Rule.
3. **Resume semantics** under batching: resume re-sends batches containing
   `pending` (and `failed`-below-cap) rows; `attempts` bumps per batch. The
   `_ACTIVE_DRAINS` registry + tracked-task lifecycle (Ruling 1) is retained —
   confirm the tracked-task drain now iterates BATCHES, not rows, and that the
   `draft→sending` CAS + registry are still the double-run guard. Confirm.
4. **Counters:** with model (A), `sent_count` means "accepted by Mailgun";
   `failed_count` = batches that errored (per-recipient); `skipped_count` =
   lapsed users filtered at claim. Acceptable, or require rename? Rule.
5. **The confirm rails, cap, dry-run, audit-no-PII, superadmin gate are
   UNCHANGED** — confirm they carry over intact.

### Deferred (unchanged): Mailgun bounce/complaint webhooks for real
per-recipient delivery status; unsubscribe/suppression (still required before any
non-transactional broadcast).

### Architect rulings on the batch revision (LOCKED 2026-07-19)

Verdict: APPROVED-WITH-RULINGS. R1-R5 override the "Send execution" section and
Rulings 1/11/12 where they conflict; MA1-MA7 are mandatory additions. Tables,
router gate, materialization, tracked-task lifecycle, `_ACTIVE_DRAINS` registry,
and `draft→sending` CAS all carry over. **No new migration** (model A).

- **R1 — Model A, no enum change.** Repurpose `sent` = "accepted by Mailgun for
  delivery" (per-batch 2xx). NOT "delivered" (that needs the deferred webhooks).
  UI/response MUST label `sent_count` as "Queued" / "Accepted for delivery",
  never "Delivered" or bare "Sent"; `BroadcastResponse` field description says so.
  No `ALTER ENUM`, no migration — that is the point.
- **R2 — Claim-BEFORE-send, per batch.** Per batch in the drain: SELECT next
  ≤`BROADCAST_BATCH_SIZE` eligible rows by id (fresh: `pending`; resume:
  `pending`+`failed` with `attempts<max`); segment re-check each (lapsed →
  claim `SKIPPED`, exclude); claim the surviving set in ONE update
  `SET status='sent', attempts=attempts+1, sent_at=NOW() WHERE broadcast_id=:id
  AND id IN (:ids) AND status=<expected>`, build `to`+vars from exactly that set,
  **commit**, THEN `send_batch`; 2xx → keep `sent`, recompute counters, commit,
  pace; non-2xx → revert that batch `sent→failed` with error, commit, pace.
  Single-instance + registry mean the SELECT set == claim set, so no per-row CAS
  needed. Accepted residual (document in code): a crash mid-call leaves an
  in-flight batch `sent` but maybe undelivered, not retried by resume;
  reconcilable via the per-batch log (MA5). Never-double-send is the invariant.
- **R3 — Resume iterates BATCHES.** Tracked-task lifecycle + registry retained as
  the double-run guard. `attempts` is batch-coarse (one transient 5xx costs one
  attempt for up to 1000 rows at once); `MAX_ATTEMPTS=3` is fine. Counters stay
  recompute-from-rows.
- **R4 — Keep DB column names**, relabel at presentation (R1). `_recompute_*`
  unchanged. `failed_count`=recipients in non-2xx batches, `skipped_count`=lapsed.
- **R5 — Rails/cap/dry-run/audit/gate unchanged.** Dry-run keeps `send_email`
  (single recipient = the superadmin, no recipient-variables, exercises the real
  prod path). See MA3.
- **MA1 — `%`-collision guard.** Translate: `html.escape(body)` then
  `.replace("{first_name}","%recipient.first_name_html%")` for HTML;
  `.replace("{first_name}","%recipient.first_name_text%")` on raw body for text.
  Mailgun substitutes recipient-vars over the WHOLE payload, so a stray literal
  `%…%` in copy/footer/shell is a hazard. Assert the outgoing payload contains
  only the two expected tokens (or escape stray `%`). Send-time assertion + test.
- **MA2 — Complete, key-matched vars map.** Every `to` address has BOTH
  `first_name_html` and `first_name_text` (fallback "there" in the map, never in
  the token). Same snapshot email string for `to` and the map key. Assert
  `set(map.keys()) == set(to_list)` before sending.
- **MA3 — Dry-run/batch byte-parity.** `render_email` stays as the single-recipient
  renderer (dry-run) AND the parity oracle. Tests: for a normal name, null→"there",
  and a `<`/`&` name, assert Mailgun-substituted html/text (simulate substitution)
  == `render_email(...)` html/text.
- **MA4 — `send_batch(to_list, subject, body_html, body_text, recipient_variables)
  -> bool`.** `raise_for_status()` → False on any non-2xx/exception, True on 2xx.
  No per-recipient parsing (none exists at call time). `recipient-variables` sent
  as `json.dumps(map)` string; `to=to_list` (repeated field); same httpx `data=`
  mechanism as `send_email`.
- **MA5 — PII-bounded batch logging.** `send_batch` logs batch size + subject +
  status ONLY, never the address list / vars / body. Emit `broadcast_batch_sent`
  / `broadcast_batch_failed` (the reconciliation signal R2's residual relies on).
- **MA6 — `BROADCAST_BATCH_SIZE`** new setting, default 1000, hard-capped at 1000
  (Mailgun limit), in `config.py`. `BROADCAST_PACING_SECONDS` now paces BETWEEN
  batches; sub-1000 audience = one call, no pacing.
- **MA7 — Materialization / `total_recipients` UNCHANGED.** Snapshot rows feed
  both `to` and the vars map. No model/constraint/index/migration change.
- **Minor (ops):** ensure `settings.email_from` Reply-To is a monitored mailbox
  (the copy says "just reply to this email").
