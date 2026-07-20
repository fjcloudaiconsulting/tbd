# Broadcast admin UI — design

Date: 2026-07-20
Status: APPROVED-WITH-RULINGS (architect, 2026-07-20), pending implementation

> Architect rulings are LOCKED at the bottom and OVERRIDE conflicting text above.

Feature A of the broadcast follow-ups (B webhooks ✅ → **A admin UI** → C API keys).
Backend is fully shipped (#553/#554/#555/#556); this is the superadmin surface
so the operator sends broadcasts by clicking instead of curl, and sees the
delivery status #556 records.

## Motivation

The broadcast backend has no UI — the Friday apology went out via raw curl. This
adds a superadmin surface to compose, dry-run, send (with the safety gates), and
watch progress + real delivery outcomes.

## Structure — a tab under Announcements

Follow the app's route-based tab pattern (`components/SettingsLayout.tsx`). Turn
`/system/announcements` into a two-tab area:
- **In-app** → `/system/announcements` (the existing announcements page, moved
  under the shared tab nav; behaviour unchanged).
- **Email broadcasts** → NEW `/system/announcements/broadcasts`.

A shared tab nav component (mirroring the `SettingsLayout` nav markup: `border-b`,
active tab `border-accent text-accent`) renders on both. Both remain
superadmin-gated exactly as the announcements page is today. No backend route
change for the tabs (pure frontend routing).

## The Email broadcasts page (`/system/announcements/broadcasts`)

Client component, `apiFetch` from `@/lib/api`, styled like the announcements
page (design tokens only, no raw palette — DESIGN.md).

### List
`GET /api/v1/admin/broadcasts` (ListEnvelope). Each row: subject; status badge
(draft / sending / completed / failed); total recipients; **Queued** count
(= `sent_count`, "accepted by Mailgun", NEVER labeled "Delivered" — Ruling R1);
and once delivery data exists, a compact breakdown Delivered `delivered_count` /
Bounced `bounced_count` (+`soft_bounced_count` soft) / Complaints
`complained_count`; created date. Row click → detail/compose panel.

### Compose (new broadcast)
Fields: `subject`, `body_template` (textarea; helper text: `{first_name}` token +
"blank lines become paragraphs"), segment shown read-only as "Active + verified
(N)" where N is the live `recipient_count` from the create/preview response.
`POST ""` creates a draft.

### Send flow (per draft)
1. **Preview** — `GET /{id}/preview`, render the returned **`text`** in a
   design-token-styled `<pre className="whitespace-pre-wrap">` (Ruling 5). Do
   NOT iframe the `html` and NEVER `dangerouslySetInnerHTML`: the app CSP
   `frame-src` is Cloudflare-only, so an `<iframe srcdoc>` is browser-blocked,
   and expanding it for a preview is an unjustified security change. The `html`
   field stays unused by the UI in v1. Note in the UI that "Send test to me"
   shows the real Mailgun-rendered HTML — that dry-run-to-self is the true
   fidelity check, stronger than any in-page frame.
2. **Send test to me** — `POST /{id}/dry-run`; on success show "test sent to
   <your email>". Enables the Send button.
3. **Send to N** — opens a **confirm modal**: shows the subject read-only and the
   recipient count prominently; operator must **type the recipient count** into a
   field to enable the final Send, plus a checkbox "This sends a real email to N
   customers." On confirm → `POST /{id}/send` with
   `{confirm_subject, confirm_recipient_count}` (subject taken from the draft, not
   retyped). Button disabled until a dry-run has happened (mirrors the backend
   `dry_run_required` gate).
4. Coded 4xx → friendly inline messages. **Read the code from `err.detail`, NOT
   `err.code`** (Ruling 7.1): `apiFetch` only populates `err.code` when `detail`
   carries a `message`, but the backend raises `detail={"code": ...}` bare — so
   `err.code` is undefined. Local helper: `err instanceof ApiResponseError &&
   err.detail && typeof err.detail === "object" ? err.detail.code : undefined`.
   Do NOT modify shared `apiFetch`. Map the six codes:
   `dry_run_required`, `confirm_subject_mismatch`, `confirm_count_mismatch`,
   `recipient_cap_exceeded`, `broadcast_not_draft`, `invalid_template_token`
   ("A '%' in the subject or body isn't allowed — remove it and try again.").

### Progress + delivery
While `status == "sending"`, poll `GET /{id}` every ~5 s; show a progress line:
Queued `sent_count` / Failed `failed_count` / Skipped `skipped_count` of
`total_recipients`. A **Resume** button (`POST /{id}/resume`) if it stalls.
`status == "completed"` stops polling. Below: the delivery breakdown (from
#556). A **View recipients** action opens `GET /{id}/recipients` (paginated) —
a table of email / status / delivery_status so the operator sees which addresses
bounced or complained.

### Honest labeling (locked)
- `sent_count` → "Queued" / "Accepted for delivery". NEVER "Delivered".
- Delivered / Bounced / Complaint come only from webhook data. A small note:
  "Delivery status populates as Mailgun reports back (requires the delivery
  webhook to be configured)."

## Types
Add to `frontend/lib/types.ts`: `Broadcast` (mirrors `BroadcastResponse`
incl. the four delivery counts), `BroadcastRecipient` (mirrors
`RecipientResponse`), `PreviewResponse`.

## Backend Minors folded (from #553, per reviewer — the UI adds the double-click surface)
1. **Concurrent double-send → 409, not 500**: in `admin_broadcasts.py` `send`,
   run the `draft→sending` CAS (rowcount==1 → else 409 `broadcast_not_draft`)
   BEFORE `materialize_recipients`, so a second concurrent `send` gets a clean
   409 instead of an `IntegrityError`→500 at the unique constraint. Add a test.
2. **Dry-run draft-only guard**: `dry_run` returns 409 `broadcast_not_draft` if
   the broadcast is not `draft` (today a completed broadcast can be re-dry-run to
   self; harmless but tidier). Add a test.
3. **Preview + list happy-path tests**: assert `GET /{id}/preview` returns the
   rendered shape and `GET ""` returns the envelope (only 403-gated today).

## Testing
- Frontend (vitest + RTL, mirror existing `frontend/tests/app/*` patterns):
  the broadcasts page renders the list; compose creates a draft; the confirm
  modal keeps Send disabled until the typed count matches AND dry-run done;
  a coded error renders its friendly message; `sent_count` renders as "Queued"
  not "Delivered"; the tab nav highlights the active tab. Mock `apiFetch`.
- Backend: the 3 Minors above (isolated `-p team-*` stack).
- Full frontend suite (`npm test`) + tsc + eslint green (the "Frontend Checks"
  CI gate runs all three).

## Out of scope
Scheduler auto-resume; unsubscribe/suppression; editing/deleting broadcasts
(create + send + observe only for v1); scheduled send-later.

## Rollout
One PR (frontend UI + the 3 backend Minors). On merge + deploy the operator has
the superadmin surface. (Delivery counts stay zero until the Mailgun webhook +
signing key from #556 are configured.)

## Architect rulings (LOCKED 2026-07-20)

Verdict: APPROVED-WITH-RULINGS. Fold R1-R7 + mandatory additions. Override
conflicting body text.

- **R1 — Shared wrapper COMPONENT, not a route `layout.tsx`.** Create
  `frontend/components/AnnouncementsLayout.tsx` mirroring `SettingsLayout`:
  renders `<AppShell>` + `<h1>Announcements</h1>` + the two-tab `<nav>` (exact
  SettingsLayout nav markup/tokens) + `{children}`; takes `activeTab`. A route
  `layout.tsx` is WRONG (it would sit outside `AppShell`, which pages render
  themselves). **Move the superadmin guard INTO the wrapper** (loading spinner;
  `router.replace("/login")` if no user; `router.replace("/dashboard")` if not
  superadmin; else `return null`) so both tabs inherit it. Refactor
  `app/system/announcements/page.tsx`: drop its own `<AppShell>`, `<h1>`, and
  guard block; wrap remaining content in
  `<AnnouncementsLayout activeTab="/system/announcements">`. Keep EVERY existing
  `data-testid` intact; update `frontend/tests/app/system-announcements-page.test.tsx`
  for the moved chrome (the only regression surface).
- **R2 — CAS-before-materialize (Minor #1) VERIFIED CORRECT.** New order:
  checks 1-6 (incl. fresh `count_segment` recompute + confirm compare) → CAS
  `draft→sending` (`rowcount!=1` → rollback → 409 `broadcast_not_draft`) →
  `materialize_recipients` → `confirmed_at`/`total_recipients` → commit →
  `launch_drain`. The loser blocks on the row lock, sees SENDING, 409s, never
  materializes → no IntegrityError → no 500. All invariants hold. Ship it +
  the concurrent-double-send test.
- **R3 — Labeling honesty (locked).** `sent_count` → "Queued"/"Accepted", never
  "Delivered"/"Sent". `completed` status = "queued to Mailgun in full", NOT
  delivered — add the note "Delivery status populates as Mailgun reports back
  (requires the delivery webhook)". Confirm-modal + success copy: "queues a real
  email to N", never "delivers".
- **R4 — Send-flow safety.** Keep the client type-the-count + dry-run-done gate
  (defense-in-depth + honest friction). ADD: disable Send while `/send` is
  in-flight (double-submit); show Preview/Dry-run/Send actions ONLY when
  `status=="draft"` (a sending/completed/failed broadcast shows progress/
  delivery, never a re-send control); on `confirm_count_mismatch` re-fetch the
  broadcast to refresh the shown count.
- **R5 — Preview = `text` in a `<pre>`, NOT an iframe** (CSP `frame-src` is
  Cloudflare-only; iframe is browser-blocked). Never `dangerouslySetInnerHTML`.
  Dry-run-to-self is the real HTML fidelity check. `PreviewResponse.html` unused
  in v1.
- **R6 — Scope create+send+observe (no edit/delete) is correct for v1.** Don't
  add edit/delete (editing post-dry-run would need to reset the dry_run gate).
- **R7 — Gaps to close:**
  1. **Read `err.detail.code`, NOT `err.code`** (apiFetch only sets `code` when
     `detail` has a `message`; backend sends `detail={"code":...}` bare). Local
     helper; don't touch shared `apiFetch`.
  2. Hardcode `segment: "active_verified"` in the POST body (backend 422s any
     other value); label "Active + verified (N)".
  3. Poll `GET /{id}` ~5s while `sending`; STOP on `completed` AND `failed`;
     clear interval on unmount. Show **Resume** on `sending` (stalled) AND
     `failed` (`resume_pending` can recover a failed broadcast).
  4. Design tokens only (No Off-Token; `check-design-tokens.sh` is CI-blocking):
     status badges reuse announcements-page token classes.
  5. Loading spinner + empty-state row ("No broadcasts yet.").
  6. CI three-gate: full `vitest run` + `tsc --noEmit` + `eslint . --quiet`.
- **Mandatory tests:** backend — concurrent double-send (200+409, one
  materialized set), dry-run-on-non-draft → 409, preview+list happy-path (only
  403-gated today), in an isolated `-p team-*` stack. Frontend — list renders;
  compose creates a draft with `segment="active_verified"`; confirm modal keeps
  Send disabled until typed count matches AND dry-run done; a coded error (via
  `err.detail.code`) renders friendly copy; `sent_count` renders "Queued"; active
  tab highlights; the refactored announcements test still passes.
- **Types** (`lib/types.ts`): `Broadcast`/`BroadcastRecipient`/`PreviewResponse`
  mirror the real `BroadcastResponse`/`RecipientResponse`/`PreviewResponse`
  incl. all four delivery counts + nullable `recipient_count`.
