# TBD-364 — `remove_member` superadmin guard + `org.member.remove.failed` audit

Status: ready to build
Ticket: https://fjconsulting.atlassian.net/browse/TBD-364
Design: two independent architects, one concede-or-defend round (2026-08-11)

## Phase 0 — premise verified by execution, not by reading

Both halves of the premise were reproduced live against `origin/main` @ `1921f6f9`:

1. An org ADMIN (non-superadmin) called `remove_member` against a platform
   superadmin whose org role was MEMBER. It **succeeded**: `is_active=False`,
   `is_superadmin=True`, `sessions_invalidated_at` stamped. No guard fired.
2. The rejoin then refused:
   `"This account holds a platform role and cannot be reactivated through an
   organization invitation. Contact platform support."` — `invitation_service.py:354`
   (TBD-351 O1). The lockout is permanent and in-app-unrecoverable.

Ticket line drift, immaterial: `remove_member` is at `:495-538` (ticket said
`:456-499`); the second bootstrap is `auth.py:3426` (ticket said `:3397`).

## The defect

`backend/app/services/invitation_service.py:495-538` enforces four guards —
self-removal `:503`, not-found `:511`, inactive early-return `:513-515`,
ADMIN-cannot-remove-OWNER `:518`, last-active-OWNER `:522-533` — and has no
`target.is_superadmin` check. A superadmin whose *org* role is MEMBER or ADMIN
trips none of them.

Reachable at `DELETE /api/v1/orgs/members/{user_id}`
(`backend/app/routers/org_members.py:288-310`), gated only by
`require_org_admin` + `require_interactive_session`.

There is no promotion endpoint: `is_superadmin=True` is only ever written at
construction (`auth.py:367`, `auth.py:3426`). No site anywhere sets an existing
row's flag to `False`.

## ⚠ The vacuity trap in this ticket's own lineage

**Do not inherit TBD-351's escalation assertion.** That fence asserts
`count(is_superadmin) == 1` after its refusal
(`tests/services/test_invitation_service.py:576-580`). Both bootstrap predicates
count `User.is_superadmin == True` with **no `is_active` filter**
(`auth.py:351`, `auth.py:3407`), so a soft-delete leaves the count unchanged and
that assertion **passes against the unfixed code here**. Copying it produces a
green fence over a live bug.

The load-bearing assertion for TBD-364 is `target.is_active is True`, read
**inside the same session before it closes** — the ConflictError path never
commits, so a post-close assertion alone also goes green against a
mutate-then-raise implementation.

This defect is a permanent denial-of-service against the platform admin. It is
**not** a privilege escalation.

## Rulings

| # | Ruling | Basis |
|---|---|---|
| F1 | Guard goes **before** the `is_active` early-return, after not-found | Both architects, independently. `admin_users_service.py:140` precedes its `is_active` precondition `:145`; `admin_org_members_service.py:134` precedes its no-op computation |
| F2 | `ConflictError(code=...)`; **all four** refusals get codes | `exceptions.py:20-30` supports `code=`; precedent `admin_users_service.py:95,140` |
| F3 | `org.member.remove.failed`, **refusal-only**, **no success rows** | Measured: `.failed` has 6 pairs incl. tenant routers `org_data.py`, `auth.py`; same-event-type-both-outcomes has exactly 1 site (`orgs.py` rename) |
| F4 | `record_audit_event` (independent session) after an explicit `db.rollback()` | `audit_service.py:130-133` names this exact case |
| F5 | Actor scalars snapshotted before `try`; target read via **columns-only** SELECT before the rollback | `rollback()` expires instances regardless of `expire_on_commit=False` (`database.py:89`); `get_db` only closes (`database.py:90-95`) |
| F6 | Ghost `org.invitation.accepted` — **out of scope**, file follow-up | It is a forward L4.4 contract, not stale docs; correcting it forces a shipped/descoped/owed ruling |
| F7 | Exactly one org-admin-reachable door: this one | Full sweep below |

### F1 — why before the early-return

`is_superadmin=True AND is_active=False` is precisely the state this bug
produces. Guarding after the early-return means the one population that is
actually locked out gets a silent `204` **and no audit row** — the fix would
suppress the very signal that reveals the lockout.

This is not an idempotency violation. Idempotency says repeating the request
does not further change state; it does not require a matching status code. A
superadmin target 409s on the first call too, so the second is byte-identical.

Deliberate secondary consequence: the superadmin guard now precedes the two
OWNER guards, so a superadmin who is the org's last OWNER is refused with the
*superadmin* reason. That is correct — report the strongest protection.

Do **not** hoist it above the self-check at `:503`; a superadmin removing
themselves should still read "You cannot remove yourself".

### F3 — why refusal-only, and why every refusal

Emitting only the superadmin refusal makes **absence** of a row uninterpretable:
an operator cannot distinguish "nobody attempted a removal" from "someone
attempted one and hit the last-owner guard". That is the same defect class as a
dishonest event name. Cost is four string constants; the `except ConflictError`
block is being written regardless and `detail.reason = e.code` is
reason-agnostic.

⚠ **The router audits UNCONDITIONALLY, not `if e.code is not None:`** as an
earlier draft of this spec said. A future `ConflictError` raised here without a
code should still produce a row (with `reason: null`) rather than vanish
silently — the code is a label on the row, not a gate on writing it. Do not
"fix" this back to a conditional.

Success rows (`org.member.removed`) are a **real** gap under CLAUDE.md's audit
convention but are out of scope: emitting one correctly forces unscoped
decisions — whether the removed member is notified (the sibling path dispatches
to the target, `admin_orgs.py:966-990`), whether the row carries before/after,
and whether `delete_invitation` (`org_members.py:164`, the other unaudited
mutation in this file) gets the same treatment. A success row without
notification parity is itself a half-fix. The name stays free for that ticket.

`NotFoundError` (404) stays unaudited here — different HTTP class, and auditing
it opens the member-enumeration-probe question.

## F7 — second-door sweep (complete)

Every site in `backend/app` that can remove a user from an org:

| # | Site | Effect | Reachable by | Guard today | Verdict |
|---|---|---|---|---|---|
| 1 | `invitation_service.py:535` ← `org_members.py:288` | soft-delete + session kill | **org ADMIN/OWNER** | **NONE** | **THE DEFECT** |
| 2 | `admin_org_members_service.py:168` ← `admin_orgs.py:853` | soft-delete + session kill | superadmin | yes `:134` | OK |
| 3 | `admin_users_service.py:241` ← `admin_users.py:394` | hard delete | superadmin | yes `:140` + code | OK |
| 4 | `user_merge_service.py:189` ← `admin_users.py:117` | hard delete of source | superadmin | partial `:81` | Report only — cannot reach count 0 |
| 5 | `admin_orgs_service.py:325` ← `admin_orgs.py:232` | hard delete of every user in org | superadmin | **NONE** | Follow-up, **P2** — see below |
| 6 | `org_data_service.reset_org_data` | preserves users (`:103`) | org OWNER | n/a | Not a door |
| 7 | `accept_invitation` `:361` | reactivation | public | refuses `:354` | The *recovery* door, closed on purpose |

No bulk member path, no scheduler job deactivating users, no org-transfer path.

**At TBD-364's threat level (org ADMIN, no platform privilege), site #1 is the
only door.**

### Site #5 rating — verified, P2 not P1

One architect rated `delete_org_cascade` a P1 escalation: it hard-deletes
superadmins with no guard, and driving `count(is_superadmin)` to 0 re-arms the
first-registrant bootstrap. **That escalation leg is wrong.** Verified
independently at file:line:

1. `admin_orgs.py:244-248` — `if org_id == current_user.org_id: 409`.
2. `models/user.py:51-53` — `org_id` is `nullable=False`, so (1) is an
   `int == int` compare with no NULL-comparison escape.
3. `admin_orgs_service.py:325` — cascade is `delete(User).where(User.org_id == org_id)`.
4. `auth/permissions.py:78,93-108` — `ROLE_PERMISSIONS` is `{}` and
   `_platform_roles` derives solely from `is_superadmin`, so `orgs.manage` is
   reachable **only** via the superadmin short-circuit.

The actor is necessarily a superadmin and their row is structurally outside the
delete set, so `count(is_superadmin) >= 1` after every call, by induction over
any sequence. Two superadmins in two orgs, A deletes B's org → 2→1. Nobody can
delete their own org. No site sets an existing row's flag to `False`, so an
actor cannot shed the flag to enter the delete set.

Real harms, for the follow-up: it deletes an unbounded number of platform-admin
rows with no signal (the typed confirmation names the *org*, not the admins
inside it, `admin_orgs.py:257-261`); and `audit_events.actor_user_id` is
`ON DELETE SET NULL` (`models/audit_event.py:163-168`), so deleting a superadmin
anonymizes their entire audit history, surviving only via the `actor_email`
snapshot. **The invariant lives in the router, not the service** — a second
caller of `delete_org_cascade` breaks the proof and nothing in the service says
so.

**Re-rate to P1 if either: (a) a second caller of `delete_org_cascade` appears,
or (b) any endpoint ships that can set an existing row's `is_superadmin` to
`False`.** (a) is covered mechanically by fence F-8.

### No in-app recovery exists for an already-damaged row

`admin_org_members_service.py:134` refuses to touch a superadmin **at all**,
including flipping `is_active` back to `True`. Combined with
`accept_invitation:354`, a victim of this bug needs direct SQL. Stated in the PR
body as a runbook; the asymmetric carve-out (permit reactivate, keep refusing
deactivate/demote) is a separate ticket.

## Changes

### 1. `backend/app/services/invitation_service.py`

Module constants beside the other module-level constants:

```python
CODE_TARGET_IS_SELF = "self_removal"
CODE_TARGET_IS_SUPERADMIN = "target_is_platform_superadmin"
CODE_OWNER_REMOVAL_REQUIRES_OWNER = "owner_removal_requires_owner"
CODE_LAST_ACTIVE_OWNER = "last_active_owner"
```

Add `code=` to the three existing raises (`:504`, `:519`, `:533`). Insert the
new guard **between `:512` and `:513`**, before the `is_active` early-return,
with a comment recording the ordering rationale and the no-undo fact.

`remove_member` keeps returning `User` — no signature change.

### 2. `backend/app/routers/org_members.py`

New imports: `structlog`, `async_sessionmaker`, `get_session_factory`,
`get_client_ip`, `audit_service`. Module `logger` + `_request_id()` helper
copied verbatim from `org_data.py:31-33`.

Handler gains `request: Request` and `session_factory: ... = Depends(get_session_factory)`.

Order is exact and load-bearing:

1. Snapshot `actor_id`, `actor_email`, `actor_org_id` **before** the `try`.
2. `except ConflictError as e:` → columns-only, **org-scoped** SELECT
   (`select(User.role, User.is_active, User.email, Organization.name)
   .join(...).where(User.id == user_id, User.org_id == actor_org_id)`), taking
   `row[0].value` for the enum.
3. `await db.rollback()`.
4. `record_audit_event(session_factory, event_type="org.member.remove.failed",
   outcome="failure", ..., detail={target_user_id, target_email, target_role,
   target_is_active, reason: e.code})`.
5. `raise HTTPException(409, detail=str(e))` — plain string, so
   `MembersSection.tsx:188-195` surfaces the new sentence with zero frontend
   change.

Columns not entity: a `Row` of plain scalars is immune to `rollback()` expiry;
`select(User)` then reading `.role` after the rollback is a `MissingGreenlet`.
Refusal path only — the happy path pays **zero** extra queries.

## Fences (revision 2 — reworked after an adversarial read + a build round)

Revision 1's fence table was **rejected**. Three of eight fences did not do what
they claimed. Everything below the fence table in rev 1 (defect analysis, F1/F3
rulings, F7 sweep, handler ordering, site-#5 re-rating) survived attack
unchanged; only this section was reworked. What changed, and why, is recorded at
the bottom — including one claim of **mine** that a build round refuted.

Service fences → `tests/services/test_invitation_service.py`; router fences →
`tests/routers/test_org_members.py`.

| # | Fence | Kills |
|---|---|---|
| F-1 | Active superadmin refused, unmutated. Parametrized over target org role ∈ {MEMBER, ADMIN}. Assert `code == CODE_TARGET_IS_SUPERADMIN`, and **inside the session** `is_active is True` and `sessions_invalidated_at is None` | the shipped defect; mutate-then-raise; a role-conditional guard; a subject swap (`current_user.is_superadmin`) |
| F-2 | **Already-inactive superadmin still refused**, does not return | **the guard placed after the `is_active` early-return.** Measured: exactly one test fails under that mutant — without this fence it is green on the entire suite |
| F-3 | *Service-level.* Superadmin who is the last active OWNER → code is `target_is_platform_superadmin`. Plus a control: clear the superadmin flag on the same fixture and assert `CODE_LAST_ACTIVE_OWNER` now fires | guard appended at the end of the guard block. The control is what proves the fixture actually reaches the last-owner branch rather than short-circuiting earlier |
| F-4 | Router: 409 + exactly one row **filtered by `event_type == "org.member.remove.failed"`**, `outcome=FAILURE`, `actor_user_id==admin.id`, `target_org_name=="Acme"`, `detail.reason`, `detail.target_role`, `detail.target_is_active is True` | service fix with no router wiring; `add_audit_event_to_session` (row discarded); entity-SELECT read after rollback |
| **F-4a** | **Harness requirement, rewritten.** `make_app` must override `get_session_factory`. **Not a vacuity risk** — see the correction below | nothing. It is a precondition, not a fence. Recorded so the next reader does not re-derive it |
| **F-4b** | **Harness fix — required for F-4 to mean anything.** `override_current_user` must resolve the actor from the **request** session (`Depends(get_db)`), not a private one | **the actor-read-after-rollback mutant.** Measured: with the stock detached-actor harness that mutant leaves the suite GREEN while production returns 500 with zero audit rows. This is the entire justification for the snapshot-before-`try` rule |
| F-5 | Parametrized over the **three router-reachable** refusal branches → exactly one row each, `detail.reason` matching by **exact `code` equality, never a message regex** | superadmin-only auditing (makes silence uninterpretable); a guard-collapse refactor. Measured: the superadmin-only mutant fails exactly 2 params |
| F-6 | Control: ordinary member still removable → 204, `is_active False`, `sessions_invalidated_at` set, **zero** `org.member.remove.failed` rows | a blunt guard; audit leaking onto the success path |
| F-7 | `ip_address` from the single helper: `PFV_RUNTIME=app_platform` + `do-connecting-ip` header → row's `ip_address` is the header value | `request.client.host` (would be `"testclient"`). The repo AST fence forbids the raw read but by its own docstring cannot catch a *wrong value*. Measured working |
| **F-8a** | **Hard-delete site allowlist.** AST-walk for `delete(User)`; assert the `(file, function)` set equals the pinned three, annotated with guard status | a new hard-delete *site* shipping unguarded |
| **F-8b** | **`delete_org_cascade` CALLER allowlist** — assert the caller set is exactly `{routers/admin_orgs.py::delete_org}` | **TBD-373's re-rate trigger (a)**, which F-8a does *not* cover — see the correction below |
| **F-9** | **Ordering fence.** Assert (AST) that in the handler's `except ConflictError` block the `db.rollback()` call precedes the `record_audit_event` call | reordering the two. Measured: nothing else pins it — every fence stays green when swapped, because on SQLite/StaticPool both sessions share one connection. On MySQL with a pending row lock it can deadlock. This is the repo's known MySQL-invisible-to-CI class |
| **F-10** | **Repair a pre-existing vacuous test.** `test_remove_member_blocks_removing_last_owner` passes an **ADMIN** actor, so `:518` fires first and its `match="owner"` regex matches *that* message. It has never executed the guard it is named after | the test's own vacuity. Re-point it at an OWNER actor and assert exact `code` equality |

The three most likely to be missed: **F-4b** (without it F-4's headline kill is
decoration), **F-9** (nothing else pins the ordering), **F-10** (a green test
lying about its own subject).

### Corrections folded in — including one of mine

1. ⚠ **Rev 1 claimed F-4 is "vacuously green" without the `get_session_factory`
   override. That is FALSE, and a build round measured it false.** Without the
   override the real factory raises an FK `IntegrityError`,
   `record_audit_event` swallows it, and **zero** rows reach the test DB — so
   `len(rows) == 1` fails **loudly**. The mechanism in rev 1 was right; the
   conclusion was inverted. The override is still required, for the opposite
   reason. *This claim was asserted without being run, which is the exact
   failure the verification protocol exists to prevent.*
2. **The real vacuity was elsewhere and rev 1 missed it entirely** (F-4b): the
   stock harness resolves `current_user` from a private session, so the actor is
   detached and `rollback()` cannot expire it.
3. **F-5's fourth branch is unconstructible via HTTP.** `last_active_owner`
   needs an actor who is an active OWNER of the org, who is therefore counted
   alongside the target, so `active_owners >= 2` always. Reachable only below
   HTTP with an inactive-OWNER actor. Three router params, not four.
4. **F-8's `is_active` half is subtracted, not fixed.** It (a) could not
   distinguish a `User` write from a `Plan`/`RecurringTransaction` write — the
   AST has no types; (b) was structurally blind to
   `admin_org_members_service.py:168` (`target.is_active = is_active`, a
   variable), **a door listed in this spec's own F7 table**; and (c) duplicated
   `tests/auth/test_sessions_invalidated_at_allowlist.py`, which already walks a
   User-unique column bidirectionally and whose allowlist already contains
   exactly the two User soft-delete doors. Cross-reference it instead.
5. **F-8 did not enforce TBD-373's trigger (a)**, contrary to rev 1 and to the
   ticket as filed. Trigger (a) is a new *caller* of `delete_org_cascade`; the
   walk finds `delete(User)` *sites*. Split into F-8a + F-8b. **TBD-373 has been
   corrected by comment.**

## Verification protocol

Every fence gets all three legs: RED before implementation → green after →
**RED again** with the named wrong implementation re-introduced → restore →
confirm green. Restore from a `cp` backup, never `git checkout --` (it reverts
the whole file and wipes uncommitted work).

A fence whose third leg cannot be demonstrated is decoration and must be
deleted or reworked — that is what happened to rev 1's F-8.

Full backend suite required. No frontend change, so no `tsc`/`vitest`/token gate
is triggered; assert that by inspecting the diff, do not assume it.

**Do not ship the F-4a diagnostic against a real engine.** The build round's
version opened a real MySQL connection and produced a
`PytestUnraisableExceptionWarning` (`Event loop is closed`) in the full run.
Assert against the `audit.record.failed` log instead.

## Out of scope — file as follow-ups

1. `org.member.removed` **success** rows + the notification-parity question, and
   `delete_invitation`'s missing audit (same file).
2. Ghost contract `org.invitation.accepted` / `org.invitation.sent` — never
   emitted; `tests/models/test_audit_event_taxonomy.py` holds the claim in place.
3. `delete_org_cascade` unguarded superadmin hard-delete — **P2**, with the two
   named re-rate triggers.
4. `admin_orgs.py:900-907` dispatches `ConflictError` → 403 via
   `if "superadmin" in msg.lower()` — a live string-match on a shared exception.
5. No in-app recovery for a soft-deleted superadmin (asymmetric carve-out).
