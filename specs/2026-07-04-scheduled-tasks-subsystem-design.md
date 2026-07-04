# Scheduled-tasks subsystem design

Date: 2026-07-04
Status: Approved (brainstorm), pending implementation plan
Owner: operator

## Purpose

Introduce time-driven automatic execution of org-scoped periodic jobs, with a
per-job opt-out, on the single-replica DigitalOcean App Platform deployment.
Today nothing runs on a schedule: recurring-due generation and billing-period
close are both user-triggered, and billing periods auto-create lazily on read
but never auto-close. This subsystem makes those actions happen automatically
while leaving each org in control, gives operators visibility into every run
via the existing audit surface, and notifies members before and after the
events that affect their budget.

Guiding constraint from the operator: lean on Redis, not a full queue system.
Do not over-engineer before customers.

## Scope (v1)

Two jobs run automatically per org:

1. Recurring-due generation
2. Billing-period close

Each is independently switchable off per org. Members receive in-app and email
notifications for meaningful events, with per-user opt-out. Operators see every
success and failure in the existing `/admin/audit` screen.

Explicit non-goals for v1: a real job queue, retries with backoff, multi-step
workflows, user-configurable run cadence, and a dedicated scheduler admin page.

Deferred to v2 (its own spec): credit-card bill / statement close. The codebase
has no persisted CC statement concept today; `cc_cycle_service` only DERIVES
which cycle a date falls in. There is no "close" action to invoke and no
open/closed state to make idempotent against. Automating it first requires
designing what "closing" or "settling" a CC statement should DO (generate the
payment transaction, mark a statement row, etc.). That is a separate subsystem
and is intentionally out of this plan. The `ScheduledJob` protocol and registry
below are built so adding it later is a drop-in.

## Execution model

A single background `asyncio` task spawned in the FastAPI lifespan (`main.py`).

- The task wakes on a fixed interval (default every 15 minutes, from config).
- On each tick it acquires a Redis lock via `SET key value NX EX <ttl>` so that
  if a second replica ever exists, only one instance runs the tick. The lock
  TTL is longer than a worst-case tick duration and shorter than the interval.
  If the lock is not acquired, the tick is skipped.
- Holding the lock, it iterates orgs and, for each enabled and due job, runs it.
- The loop is wrapped so a single job failure never kills the ticker: failures
  are caught, recorded (audit + log), and the loop continues to the next org/job.

Why this over the alternatives: no new infrastructure, no external cron to
manage, and the Redis lock future-proofs a horizontal scale-out. Because "due"
is derived from durable domain state (see each job), not from the tick firing,
a missed window during downtime is simply caught up on the next boot, and a
double-fire is a no-op.

Config (env, `config.py`):

- `SCHEDULER_ENABLED` (default true) — global kill switch for the whole loop.
- `SCHEDULER_TICK_SECONDS` (default 900).
- `SCHEDULER_LOCK_TTL_SECONDS` (default 600).

## Job framework

A small registry in a new `app/services/scheduler/` package. Each job implements
a common protocol:

```
class ScheduledJob(Protocol):
    job_type: str                     # e.g. "billing_close"
    setting_key: str                  # OrgSetting key gating this job
    def is_enabled(self, org, settings) -> bool
    async def is_due(self, db, org, today) -> bool
    async def run(self, db, org, today) -> JobResult
```

`JobResult` carries an outcome (success / failure / noop), a counts dict for the
audit `detail`, and an optional error string. A common runner does, per tick:

```
for org in active_orgs:
    for job in REGISTRY:
        if not job.is_enabled(org, settings): continue
        if not await job.is_due(db, org, today): continue
        result = await job.run(db, org, today)   # each in its own txn
        record_run(org, job, result)             # audit only if meaningful
        maybe_notify(org, job, result)
```

Each job runs in its own transaction/session scope so one org's failure does
not roll back another's work. Idempotency is a hard requirement of every `run`.

### Job 1: Recurring-due generation

- `setting_key`: `scheduler.automate_recurring_generation`
- Due: any active recurring template for the org has `next_due_date <= period_end`
  of the current cycle window (mirrors the existing `generate_due_transactions`
  gate). In practice checked daily.
- Run: call the existing `recurring_service.generate_due_transactions`. It is
  already idempotent (advances `next_due_date`) and catch-up-safe.
- Result: counts of generated / settled / pending. `noop` when zero generated
  and zero settled.

### Job 2: Billing-period close

- `setting_key`: `scheduler.automate_billing_close`
- Due: today has reached the org's `billing_cycle_day` and the current open
  period still starts before that boundary (that is, a close for this boundary
  has not already happened). Anchored to the configured day, not to "today", so
  catch-up after downtime lands on correct boundaries.
- Run: call `billing_service.close_period` with `close_date = cycle_day - 1`
  (the day before the boundary), which opens the new period on `cycle_day`.
  Idempotent via domain state: if a period boundary already exists at that date,
  the job is a noop.
- Result: closed period id, new period id. `noop` when nothing to close.

Note on the manual model: closing was previously a deliberate act ("salary came
in today, close yesterday"). Automation cannot observe salary arrival, so it
anchors to the org's configured `billing_cycle_day`. Orgs that treat close as a
ritual turn this job off.

### Job 3 (deferred): Credit-card bill close

Out of scope for v1 (see Scope). Added later as a new registry entry once the CC
statement-close concept is designed and built.

## Toggles and config (per org)

Stored as `OrgSetting` rows under a new `scheduler.` namespace, distinct from the
RESERVED `feature.` namespace. These keys are read and written exclusively
through the scheduler's own typed accessors and a dedicated scheduler-settings
endpoint (validated: three bools plus a bounded int). They are NOT exposed
through the generic user-facing settings writer, so no change to that writer's
reserved-namespace guard is needed.

- `scheduler.automate_recurring_generation` — bool, default true
- `scheduler.automate_billing_close` — bool, default true
- `scheduler.billing_close_reminder_lead_days` — int, default 3

(`scheduler.automate_cc_close` is reserved for the deferred CC job; not written
or read in v1.)

Defaults are ON so behavior is automatic out of the box; an org opts out per job.
Reading an unset key returns the default (no backfill migration needed).

Admin UI: surface the four settings on the org's existing settings/admin screen
(the same surface that already exposes org-level toggles). Reuse the existing
settings form patterns. No new page.

## Observability (reuse audit_events + /admin/audit)

No new table and no migration. Each meaningful job execution writes one
`audit_events` row through the existing `audit_service`:

- `event_type`: `scheduler.<job_type>.<outcome>`, e.g.
  `scheduler.billing_close.success`, `scheduler.recurring_generation.failure`.
- `outcome`: existing `AuditOutcome` success / failure.
- `actor_user_id`: NULL (system event). `actor_email`: the sentinel `"system"`
  (mirrors the actorless/superadmin precedent; `actor_email` is non-null).
- `target_org_id` / `target_org_name`: the org (survives org wipe).
- `detail`: counts and, on failure, the error string.

No-op ticks (nothing was due) write NOTHING to `audit_events`; they emit only a
structured `scheduler.*` log line. This keeps the audit table free of daily
noise while still recording every real success and failure.

`/admin/audit` already lists audit events; add the new `scheduler.*` event types
to its event-type filter options so an operator can filter to scheduler runs.

Structured logging: emit `scheduler.tick.start`, `scheduler.tick.skip_locked`,
`scheduler.job.start`, `scheduler.job.success`, `scheduler.job.failure`,
`scheduler.job.noop`, `scheduler.tick.complete` for grepping.

## Notifications (reuse the existing dual-channel stack)

All notifications go to ALL org members via
`notification_service.dispatch_notification` fanned across the org's users, in
the existing `org_activity` NotificationCategory. Members opt out through the
existing per-user `email_org_activity` / `in_app_org_activity` preferences, so
no new preference plumbing is required. New event_type strings are added for
correlation with audit rows.

Post-run confirmations are RESULT-GATED: they fire only when the job actually
did something (result outcome is success with non-zero effect), never on noop.
This prevents daily "generated 0" spam.

1. Recurring-due generation confirmation — when generated or settled > 0.
2. Billing-period pre-close heads-up — fires `billing_close_reminder_lead_days`
   before the org's `cycle_day`. Deduplicated per period by checking for a prior
   `scheduler.billing_close.reminder` audit row for this org and target period,
   so the multi-day lead window sends it only once.
3. Billing-period close confirmation — after a successful auto-close.

New template functions in `notification_templates.py`, each returning the
existing `(title, body, link_url)` shape, matching the sibling templates.

The pre-close reminder writing a `scheduler.billing_close.reminder` audit row is
the only case where a "reminder" (not a job run) produces an audit row; it exists
specifically to serve as the durable dedup marker and doubles as operator
visibility that the heads-up was sent.

## Data model and migrations

- No new tables. `OrgSetting` is key-value, so the four toggles need no schema
  change. `audit_events` and the notification tables are reused as-is.
- No new NotificationCategory (reuse `org_activity`).
- New string constants only: `scheduler.*` audit event_types and notification
  event_types.

## Testing (TDD, isolated -p team-* stack)

Backend unit/integration tests:

- Per job `is_due`: fires exactly at the boundary, not before; catch-up after a
  simulated downtime gap closes the correct anchored boundary (not "today").
- Per job idempotency: running twice in a row produces a noop the second time
  (no double period close, no duplicate transactions).
- Toggle gating: job disabled for an org -> not run, no audit row.
- Redis single-runner: with the lock held, a concurrent tick is skipped.
- Reminder dedup: the pre-close reminder sends once across multiple ticks within
  the lead window (asserts a single audit row and a single dispatch).
- Notification result-gating: noop run dispatches nothing; meaningful run
  dispatches to all members; a member with `org_activity` opted out receives
  neither channel.
- Audit content: success and failure rows carry the right event_type, outcome,
  target_org_id, and detail; no-op writes no audit row.
- Failure isolation: a job that raises is caught, recorded as failure, and does
  not prevent the next org/job from running.

Frontend tests:

- Org settings screen renders and toggles the four `scheduler.*` settings.
- `/admin/audit` filter includes and filters by the `scheduler.*` event types.

## Open questions

None blocking. Cadence, retries, and a dedicated scheduler admin page are
deliberately deferred as non-goals for v1.
