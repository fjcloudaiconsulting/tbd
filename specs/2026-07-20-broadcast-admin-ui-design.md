# Broadcast admin UI — design

Date: 2026-07-20
Status: approved (design), pending spec review

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
1. **Preview** — `GET /{id}/preview`, render the returned `html` in a sandboxed
   frame (or the `text` in a `<pre>`), so the operator eyeballs the real content.
2. **Send test to me** — `POST /{id}/dry-run`; on success show "test sent to
   <your email>". Enables the Send button.
3. **Send to N** — opens a **confirm modal**: shows the subject read-only and the
   recipient count prominently; operator must **type the recipient count** into a
   field to enable the final Send, plus a checkbox "This sends a real email to N
   customers." On confirm → `POST /{id}/send` with
   `{confirm_subject, confirm_recipient_count}` (subject taken from the draft, not
   retyped). Button disabled until a dry-run has happened (mirrors the backend
   `dry_run_required` gate).
4. Coded 4xx → friendly inline messages, mapped from `detail.code`:
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
