# TBD-362 — Operator recovery for an unreachable email address

Status: accepted, 2026-08-23. Two independent architects, a concede-or-defend
cross that produced a POSITION SWAP on two of three divergences, a build-it
measurement round against a real MySQL 8.4 stack, and a final ruling round in
which both architects converged.

⚠⚠ **READ THIS BEFORE ANYTHING ELSE.
`specs/2026-05-22-l4-4-admin-slices.md:325-348` ALREADY DESIGNED THIS ENDPOINT
AND IS BROKEN.** It is the obvious prior art an implementer will find. It says
the endpoint "DOES NOT mutate `users.email` yet. Mints an `email_verify` token
for the user with the NEW email baked in" and never mentions `pending_email`,
because it predates TBD-361 by three months. Implemented literally today, the
token reaches `backend/app/routers/auth.py:2371-2379`:

```python
promoting = (user.pending_email is not None and token_email == user.pending_email)
if not promoting and token_email != user.email:
    raise HTTPException(400, "Invalid or expired verification token")
```

`pending_email` is `NULL`, so `promoting` is `False`; the new address is not
`user.email`, so it **400s on every click, forever**. A total dead end that
passes any "200 returned, mail dispatched" test. Fence F2 exists to catch it.
Annotate that spec; do not follow it.

## The premise, verified

Verified at real file:line, re-anchored to `main` @ `e9db50ee`. The ticket's own
line numbers are stale; every one still lands on the right symbol, so the
premise survives and only the citations rotted.

⚠ This document was first written against `0c69fdd0`. TBD-353 (#695) then added
~347 lines to `backend/app/routers/auth.py`, shifting every citation past ~1890
by roughly +60. All have been re-derived. **If `auth.py` moves again, re-derive
before trusting a number here** — this spec's own finding 3 is that stale
citations are what rots a brief.

* `email_verified` has **no operator writer anywhere**. Across the admin
  modules it appears only as reads and one filter: `admin_orgs.py:956`,
  `admin_orgs_service.py:230`, `admin_users_search_service.py:93` and `:184`,
  `admin_org_members_service.py:97`.
* `resend_verification_public` (`auth.py:2412`) looks the user up by username
  OR email and re-sends to the **stored** address, so it cannot reach a typo'd
  or bouncing one.
* `POST /auth/login` 403s unverified accounts unconditionally at `auth.py:516`.
* Prerequisites TBD-361 and TBD-344 have both shipped.

## The ruling

**The operator writes exactly one column, `users.pending_email`. The user proves
control by clicking. The existing `verify_email` → `_promote_pending_email` path
does the rest. The `email_verified` writer set gains ZERO members.**

The ticket's DoD asked for an eighth `email_verified` writer and framed that as
the central security concern. Under TBD-361 that concern **evaporates** rather
than being managed: the operator's new power is not "assert a proof", it is
"make the gesture the locked-out user can no longer make for themselves."

### Scope guard: refuse a target that is already verified

`409 user_already_verified`, alongside the agreed `409 target_is_superadmin`
and `409 user_inactive`.

This was the one genuinely contested question. It went to a build-it round
because the architects swapped positions, and the measurement is recorded below
because the reasoning is not reconstructible from the code alone.

⚠⚠ **THE LATCH ARGUMENT BELOW IS A PROPERTY OF THE CODE. IT WAS ALSO CHECKED
AGAINST THE DATA, AND THOSE ARE DIFFERENT CLAIMS.**
`backend/alembic/versions/018_user_profile_fields.py` added `email_verified` on
2026-04-10 as `nullable=False, server_default="0"` with **no backfill**, and the
local-login gate landed twenty days later (`d98ed4b3`, 2026-04-30). So rows
predating the column were stamped `0` while owning arbitrary financial history,
and rows created inside that twenty-day window could log in freely and create
more. For those rows `email_verified=False` implies **nothing** about sessions
or data, and they would sit inside this guard's accepted set.

Measured on production, 2026-08-24:

```sql
SELECT COUNT(*) AS locked_out_active FROM users WHERE email_verified=0 AND is_active=1;
-- 0
```

The cohort is **empty**, and because nothing anywhere writes `email_verified =
False`, no existing row can enter it. So the guard's basis holds — but it holds
**by measurement, not by construction**, and this is the precondition it rests
on. ⚠ A bulk import, a restore from a pre-2026-04-30 dump, or any backfill that
creates active rows with `email_verified=0` VOIDS it. Re-run the query before
trusting this section again.

**Why the guard is safe to have.** `email_verified` is a **one-way latch**.
Every write in `backend/app/` sets it `True`; the only `False` is the
creation-time value at `auth.py:390` (`email_verified=is_first_user_setup`),
inside the `User(...)` constructor, never a transition.

⚠ **The next step is conditional, and the condition is the measurement above.**
Combined with the unconditional login 403, `email_verified=False` implies *never
held a session* implies **owns no user-created data** — but only for rows
created after the gate landed on 2026-04-30. For anything older the implication
does not hold at all, which is why the cohort had to be measured rather than
argued. Given that measurement, the guard's accepted population today is a
username and an empty organization. An attacker-operator cannot push a
protected target into the accepted set, and `user_merge_service.py:183-184`
only ever pushes targets *into* protection.

**Why the guard is not too costly.** `PUT /users/me` (`users.py:161-186`) has
**two** proof-of-presence branches: `password_set=True` supplies
`current_password`; `password_set=False` supplies a `stepup_token` minted by
the SSO step-up callback, which pins `google_email == user.email`
(`auth.py:4466`). So every verified user has a working self-serve path through
one branch or the other, and the population the guard denies is a genuine
two-failure conjunction: **lost inbox AND lost credential**.

⚠ The build-it round initially reported that SSO users were structurally
stranded. That was a **measurement hole**: it drove `PUT /users/me` with
`current_password` and with no proof at all, got 400 both times, and read that
as the absence of a remedy. It never exercised the `stepup_token` branch. Do
not re-derive that conclusion.

### Rejected: no scope guard

Ships the platform's **first** superadmin account-takeover primitive. Confirmed
end to end by execution against a real stack, on an account holding a balance
of 1234.56:

```
repoint -> click at the new inbox -> _promote_pending_email
   -> POST /auth/forgot-password (new address) -> reset token ISSUED
   -> POST /auth/reset-password -> attacker login 200
   -> attacker reads the victim's accounts: [{"balance":"1234.56"...}]
victim login at the OLD address -> 401
```

`forgot_password` matches `User.email` and gates only on `is_active`
(`auth.py:1951-1956`); `reset_password` then flips `password_set = True`
(`auth.py:2010`), so the chain completes **even against an SSO-only account**,
converting it into a password account the attacker owns. Verified that
`users.reset_credentials`, `users.impersonate` and `users.invite` all have
**zero call sites** today, so this would genuinely be the first such primitive,
not an addition to an existing one.

### Rejected: refuse when `email_verified AND password_set`

Proposed by the build-it round as a middle line. It discriminates on
**credential type** while claiming to discriminate on **recoverability**:

* it refuses users who genuinely cannot self-serve (password user, forgot the
  password, dead inbox), and
* it accepts users who can (SSO user with an intact Google identity),

while handing the takeover chain to the entire federated-IdP population. And it
buys that population no containment: after a malicious promotion `users.email`
no longer matches their Google address, so their next SSO sign-in falls through
`auth.py:3893` and mints them a **new empty account and org** — locked out
exactly as hard as with no guard at all.

## What ships

### 1. `POST /api/v1/admin/users/{user_id}/email-change`

Lives in `backend/app/routers/admin_users.py` (already cross-org, already
`users.*`-permissioned, and `/admin/users/[user_id]` is where the operator
already reads `Email verified: No`). **Not** `admin_orgs.py`, which is
org-scoped under `orgs.manage`.

```
dependencies=[Depends(require_interactive_session)]
actor: User = Depends(require_permission("users.reset_credentials"))
@limiter.shared_limit("10/hour", scope="admin_users.email_change")
```

`users.reset_credentials` already exists at `permissions.py:38` with zero call
sites — minted by the L4.4 spec and never wired. `ROLE_PERMISSIONS` is `{}`
(`permissions.py:79`), so the `is_superadmin` short-circuit is the only grant.
`require_interactive_session` bars PATs, matching `merge_users` and
`delete_user`.

Request: `new_email`, `new_email_confirm`, `reason` (required, 4..200).

`reason` is required because there is no user consent anywhere in this request,
so the forensic note is the only contemporaneous account of why. The double
entry is because the operator is fixing a typo and a typo in the fix is the
obvious failure mode.

Preconditions, in order, each audited:

| Condition | Status | code |
|---|---|---|
| target missing | 404 | `user_not_found` |
| normalized addresses differ | 400 | `emails_do_not_match` |
| `target.email_verified` | 409 | `user_already_verified` |
| `target.is_superadmin` | 409 | `target_is_superadmin` |
| `not target.is_active` | 409 | `user_inactive` |
| `normalize_email(new) == normalize_email(target.email)` | 409 | `email_unchanged` |
| another row holds it | 409 | `email_already_in_use` |

⚠ **There is no `400 invalid_email`.** `normalize_email` is
`value.strip().lower()` (`user_service.py:29-36`) and **cannot reject** — its
own docstring says incoming values are already validated by Pydantic. With
`new_email: EmailStr`, a malformed address is a FastAPI **422** raised before
the handler runs, so there is no handler-side 400, no audit row, and no such
code on the wire. The L4.4 spec published that row and it was wrong there too.
422 is **unaudited by construction** (it never reaches the handler); say so, so
nobody later reads it as a missing failure row.

⚠ **`email_unchanged` compares NORMALIZED values, on both sides.**
`_promote_pending_email`'s own comment (`auth.py:2095-2103`) records that
mixed-case `users.email` rows genuinely exist in production, because the old
request path wrote `body.email` raw. With a byte comparison, a target stored as
`Foo@Bar.com` and an operator typing `foo@bar.com` does **not** trip
`email_unchanged`; on MySQL's `utf8mb4_0900_ai_ci` the advisory uniqueness
SELECT then matches the target's own row and returns a misleading
`email_already_in_use`, while on SQLite — every backend shard except
`Migration Checks` — it matches nothing and the endpoint **arms the
self-referential promotion this design refutes by execution**. The advisory
SELECT must also carry `User.id != target.id`. F5's `_unchanged_email` leg
needs a mixed-case-stored parameter or it passes green against the wrong
implementation on the shards.

`409 target_is_superadmin` is not in the ticket and is load-bearing: without
it, superadmin A repoints superadmin B's address at an inbox A controls and
owns the platform's most privileged account. Precedent:
`admin_users_service.py:140`, `admin_org_members_service.py:134`,
`invitation_service.py:354` all refuse to mutate a superadmin.

`409 email_unchanged` closes a real defect: `promoting` is computed before any
equality check, so repointing to the target's own current address takes the
**promoting** branch, writing `sessions_invalidated_at` and a
`user.email.changed` audit row whose `old_email == new_email`. Fix it at the
admin endpoint only. ⚠ The `_promote_pending_email` half is **not reachable
from `PUT /users/me`** and is therefore NOT filed: `email_changing`
(`users.py:124-126`) normalizes both sides, so a self-addressed change is
`False`, the `cancelling_pending` branch (`:137-141`) clears the claim instead,
and the whole two-phase block is skipped. This endpoint would be the **first**
writer able to reach that state, which is exactly why the guard is required
here.

The uniqueness check is **advisory**, deliberately — but ⚠ model it on the
**promote-time** select, which already carries `User.id != user.id`, NOT on
`users.py:193-203`, whose `select(User).where(User.email == new_email_norm)` has
no id guard and is the version the `email_unchanged` fix above exists to refuse. The binding check is re-run at promote time with its
`IntegrityError` → 409 backstop. ⚠ Do **not** add a unique index on
`pending_email`: CLAUDE.md forbids it and it hands out an address-squatting
primitive.

Side effects, and nothing else:

```python
previous_pending = target.pending_email          # snapshot for the audit row
target.pending_email = new_email_norm            # the ONLY column written
await db.commit()
token = create_email_verification_token(target.id, new_email_norm, admin_initiated=True)
background_tasks.add_task(send_verification_email, new_email_norm, token)
```

⚠ Mint from the **normalized stored value**, never from `body.new_email`. The
promote-time guard compares byte-exactly, so minting from raw input yields a
link that 400s forever for any operator who types mixed case — the hazard
`users.py:222-226` documents.

It does **not** write `email_verified`, `users.email`, or
`sessions_invalidated_at`. `backend/tests/auth/test_sessions_invalidated_at_allowlist.py`
is a function-granular AST fence over that last set and **gains no entry**.

### 2. `DELETE /api/v1/admin/users/{user_id}/pending-email`

Same gating. Idempotent `200 {"cleared": bool}`. Clears `pending_email` and
nothing else. Emits `admin.user.email_change.cancelled`. ⚠ Its user-side sibling
returns **204** (`users.py:357`); the divergence is deliberate — the operator
needs to know whether anything was actually cleared — and is stated so it is not
"harmonised" away.

⚠ CLAUDE.md's `pending_email` bullet enumerates **reasons**, not functions, and
this design adds **two**: this admin cancel, and the provenance abort in §5b
(which routes through `_abandon_pending_email`). So the corrected count is
**six**, not five. Documenting only this one leaves the provenance abort
unlisted, which is precisely the drift that bullet exists to stop.

It is not optional. The typed confirmation only *prevents* a mistyped address.
If the operator mistypes the **correction** and mails a live promotion link to
an attacker-owned inbox, the remedies without a cancel are: wait out the 24h
window with a live takeover link in a stranger's inbox; overwrite with a third
address, which revokes the bad link only by minting another one at an address
the operator by hypothesis does not have; or direct SQL.

⚠ **"Just overwrite the claim with the target's own `users.email`, which is
inert" is WRONG and was refuted by execution.** That write makes `promoting`
evaluate `True` for any live register-minted bootstrap token, so the click
drives the full promotion path: `sessions_invalidated_at` set for a change that
did not happen, a `user.email.changed` audit row with `old_email == new_email`
— the exact self-referential row TBD-361 warns against — and two "your email
changed" notices to the same inbox. It manufactures a false completion record.

### 3. Claim provenance lives in the TOKEN, not a new column

⚠ This closes the one door the guard leaves open. The guard reads
`email_verified` at **trigger** time; the claim redeems up to 24 hours later,
and there are **four** arms by which an unverified target becomes verified in
between — the registration link (`auth.py:2385`), Google sign-in on the
existing row (`auth.py:3870`), invitation accept (`invitation_service.py:384`)
and admin merge (`user_merge_service.py:184`). The bootstrap arm deliberately
does **not** clear `pending_email` (`auth.py:2379-2386`), so the operator's
link stays armed and still promotes on a now-verified, now-loginable account.

`create_email_verification_token` (`security.py:253-269`) already mints a signed
JWT whose `email` claim is the provenance carrier. Add **one** claim —
admin-initiated — and refuse in `_promote_pending_email` when an admin-initiated
token meets a row that has since become verified.

⚠ Do **not** also carry a `verified_at_mint` snapshot. The endpoint refuses
verified targets, so that claim is always `False` and the redeem check reduces
to `admin_initiated and user.email_verified`. A fence asserting it "works" would
be vacuous by construction.

⚠ A token minted **before this ships** carries no `admin_initiated` claim and
correctly fails **open** into the user-initiated path. That is the right
direction — no admin-initiated token existed before this ships — and the claim
cannot be stripped without breaking the HS signature.

**Rejected: a `users.pending_email_admin_initiated` column** cleared at all four
existing clear sites. It needs a migration, touches four sites, and a flag that
must be cleared in four places fails silently if one is missed — a stale `True`
then refuses a legitimate claim, a new failure mode with no counterpart in the
token approach. Three of the four sites are already redundant, because
`promoting` pins the token to the **live** column value, so a claim the user
overwrites or cancels makes the admin token inert on its own. Exposure is
bounded by the token's 24h TTL either way.

### 4. Notifications

* **Old-address alert, sent unconditionally** — but ⚠ **NOT by reusing
  `_tpl_user_email_change_requested_old_address`.** That template
  (`notification_templates.py:180-206`) says *"cancel the pending change in
  Settings, reset your password, and contact support"* and links to
  `/settings`. Every target of this endpoint is `email_verified=False` and
  therefore 403s at `auth.py:516`, and `DELETE /users/me/pending-email` sits
  behind `require_interactive_session`. So the reused copy instructs a
  locked-out victim to cancel in Settings, which is behind a login they cannot
  pass, and to reset their password — which is NOT behind a login
  (`forgot_password` is public and gates only on `is_active`) but is useless
  anyway, because the reset mail goes to the same dead `users.email`. The in-app
  SECURITY row is equally unreadable to them. Net in-app mitigation
  would be **zero**.
  Mint an **admin-initiated** template instead, as the L4.4 spec did: name the
  acting operator, state that the account is locked out, say explicitly not to
  click the link if this was unexpected, and point at support rather than
  `/settings`.
  ⚠ It fires **even when the current address is unverified**. That looks
  wasteful and is not: "typo'd" and "attacker-chosen" are indistinguishable to
  the system, and where the address is live this is the only out-of-band signal
  the target gets.
  ⚠ This also corrects §6's stated reasoning: "an operator can retry silently
  after a victim cancels" describes an event this population cannot perform.
  Keep the §6 audit row — it is right for the general case and good hygiene —
  but not for that reason.
* **In-app SECURITY row** via `dispatch_notification_best_effort`, gated on
  `audit_event_id is not None` per the locked rule at `admin_users.py:512-524`.
* ⚠ **No credential is ever mailed to `pending_email`.** The verification link
  is a proof-of-control challenge and grants nothing unless the recipient
  controls the mailbox. `forgot_password` stays `users.email`-only.

### 5. Audit

Reuse the **already-reserved** `admin.user.email_change.triggered`
(`audit_event.py:96`, fenced by `test_audit_event_taxonomy.py`). Do not mint a
new string for the trigger. Correct that docstring paragraph — it describes the
pre-TBD-361 mechanism.

⚠ **Two genuinely new strings do arrive and both must be documented in the same
PR**: `admin.user.email_change.cancelled` (§2) and `user.email.change_cancelled`
(§6). `test_audit_event_taxonomy.py` only asserts that documented strings are
*present* in `audit_event.__doc__` — it is one-directional, so nothing fences a
new one. Add both to that docstring alongside the correction above, or they ship
undocumented and unfenced.

⚠ **`target_org_id` is read off the TARGET and must be set.** The reserved
contract specifies it, and `/admin/audit`'s only org filter is `target_org_id`
(`admin_audit.py:36`). An implementer copying `admin_users.py::merge_users`,
which passes `None`, produces rows invisible to every org-scoped audit query.

`detail` carries `target_user_id`, `target_email_old`, `target_pending_email`,
`previous_pending_email`, `reason`, `kind`. `target_email_old` matters because
promotion overwrites `users.email` and nothing else preserves the typo.
`previous_pending_email` matters because this write **is** the "overwrite by a
later request" clearer, and without it a destroyed claim leaves no trace.

Failure rows: `admin.user.email_change.failed`, `outcome="failure"`, written on
the independent session **after `await db.rollback()`**, carrying the
pre-refusal snapshot. A refusal row saying only "409" cannot distinguish "this
account was already locked out" from "an operator just attacked an active
superadmin". ⚠ `404` **is** audited here, diverging from `delete_user`
(`admin_users.py:434-437`): probing this path carries an attacker-supplied
destination address, where the delete path carries no payload.

⚠ **`actor_email` is the SUPERADMIN's**, diverging from the self-initiated
convention. `audit_events` has no `target_user_id` column, so `actor_email` is
the only identity column; on an admin-triggered row the reader's question is
"which operator", and the target's old address survives in `detail`.

### 5b. The promote-time abort of an admin claim is silent — accepted, stated

When the provenance check refuses, `_abandon_pending_email` (`auth.py:2292-2307`)
clears the claim and `verify_email` returns a generic refusal. No row names the
admin claim that just died, and the operator gets no signal that their repoint
evaporated. Accepted for v1: the operator sees the claim gone on next load, and
`_abandon_pending_email` (`auth.py:2292`) has exactly **two** callers — the
taken-conflict path (`:2120`) and the `IntegrityError` backstop (`:2171`) — both
of which would inherit any writer added there.
Stated so it is a decision, not an oversight.

### 6. Fix `cancel_pending_email` (`users.py:360-386`)

It writes **no audit row** — verified. Add `user.email.change_cancelled`.

⚠ **Its justification is NOT "the target's only defence".** An earlier draft
said that, and it is refuted: every target of this endpoint is unverified, so
they 403 at login (`auth.py:516`) and cannot reach
`DELETE /users/me/pending-email`, which sits behind `require_interactive_session`
(`users.py:355-358`). This population cannot cancel at all, so "an operator can
retry silently after a victim cancels" describes an event that cannot occur
here. The row is still right — it closes the gap for every OTHER caller of that
endpoint, where a live session (including a hijacked one) can void a claim with
nothing in `/admin/audit` — but not for this reason.

⚠ Its **absence of re-auth is deliberate and correct** and must not be
"fixed" — its docstring argues it, and demanding a password to undo a mistake
is the shape that made the original defect unrecoverable. The gap is the
missing audit row only.

## Fences

Each names the wrong implementation it kills. Every one gets
inject-and-confirm-red.

**F1 `test_endpoint_writes_pending_email_only`** — after 200, re-read the row
in a **fresh session as scalars** (never the ORM instance the request touched —
an identity-map snapshot is a tautology) and assert all four: `email_verified`
still `False`, `email` still the typo, `sessions_invalidated_at` unchanged,
`pending_email` set. Four, because a handler getting three right is the
half-fix. *Kills* the ticket's literal ask.

**F2 `test_minted_token_actually_promotes_end_to_end`** — the highest-value
fence. Call the endpoint, **read the token off the mocked
`send_verification_email` dispatch** (never construct it — a self-constructed
token fences the test's own arithmetic), POST it to
`/api/v1/auth/verify-email`, assert promotion. *Kills* the stale L4.4
implementation, which returns 200, dispatches mail, and produces a link that
400s forever.

⚠ Build notes, from a reviewer who traced them: F2 must patch
`send_verification_email` **in the new module's namespace** and let the test
client drain background tasks. F3's "200 after the click" leg needs the target
seeded `password_set=True` with a known password, or it is unreachable for an
SSO-shaped row.

**F3 `test_login_still_403s_before_the_click`** — both halves: 403 immediately
after the operator acts, 200 after the click. The second half alone is
satisfied by the wrong design. *Kills* any exemption sneaked into `auth.py:516`.

**F4 `test_org_admin_cannot_call_it`** — table-driven over `Role.OWNER/ADMIN/
MEMBER` × {same org, different org}, all `is_superadmin=False`. Assert **403
exactly**, not "not 200" (a 404 also satisfies "not 200" and means the router
was never mounted — a vacuous pass), the row byte-unchanged, and **no audit
row**. ⚠ Must run against the real `app.main:app` dependency graph, **not**
`make_test_app(...)` — but note the hazard sits on the SIBLING leg, not this
one. `require_permission` depends on `get_current_user` (`permissions.py:114-127`),
so under `make_test_app(current_user=org_admin)` the gate does fire and "403
exactly" still catches an omitted dependency. The vacuous one is
`test_pat_cannot_call_it`: `tests/factories/app.py:139-147` and `:163-167` stamp
`request.state.auth_method = "jwt"` unconditionally, so a PAT request cannot be
constructed there at all and the test passes green forever. Build both legs on
the real app.

**F5 `test_refuses_verified_target`** (+ `_superadmin_target`, `_inactive_target`,
`_unchanged_email`) — 409, **and** `pending_email` not written, **and** no mail
dispatched. A handler that commits then raises leaves the claim live.

**F6 `test_admin_token_is_refused_after_the_row_becomes_verified`** — the
provenance fence. ⚠ `_promote_pending_email` (`auth.py:2146`) is a **fifth**
site that verifies an existing row, excluded from the four arms only because it
sets `pending_email = None` in the same transaction (and `_abandon_pending_email`
clears it on the `IntegrityError` path). Say that in the test, or a refactor
that stops clearing reopens the arm silently. The merge arm needs a **same-org**
verified source, since `merge_users` refuses cross-org at
`user_merge_service.py:76-80`. Repoint an unverified target, verify it through the
**bootstrap arm**, then redeem the operator's link: refuse. *Kills* the
trigger-time-only check. Parametrize over all four verification arms.

**F7 `test_confirmation_mismatch_refused_after_normalization`** —
`Foo@x.com`/`foo@x.com` → **200**; `foo@x.com`/`fooo@x.com` → **400**. *Kills
both* a byte-equality check (which rejects a legitimate case difference and
trains operators to paste both fields, defeating the confirmation) and no
comparison at all.

**F8 `test_overwrite_records_the_claim_it_destroyed`** — seed a live claim,
retarget, assert `previous_pending_email` in the audit detail.

**F9 `test_audit_on_every_failure_path`** — parametrized over all failure codes.
⚠ Assert `outcome == "failure"` explicitly; a default-outcome write passes a
row-exists check.

**F10 `test_mail_goes_only_to_the_new_address_at_request_time`** — exactly one
verification dispatch, to the new address; no password-reset or login-link
sender called at all; **paired** with an assertion that promotion still mails
both old and new (`auth.py:2266-2287`), or the fence is satisfiable by deleting
the downstream notification instead.

**F11 `test_email_verified_writer_set_is_closed`** — an AST fence modelled
function-for-function on `test_sessions_invalidated_at_allowlist.py`. Collect
every `(file, function)` assigning `email_verified` and assert **set equality**,
failing in **both** directions. ⚠ **Parse, never grep** — this spec and the
handler's comments contain the string and would satisfy a text search.

⚠⚠ **The allowlist has SIX entries, not eight.** The model test's elements are
`(file, function)` pairs and its docstring is explicit that multiple writes
inside one function count as one entry. The eight write sites collapse:

| entry | sites |
|---|---|
| `routers/auth.py::register` | 390 |
| `routers/auth.py::_promote_pending_email` | 2083 |
| `routers/auth.py::verify_email` | 2322 |
| `routers/auth.py::google_callback` | 3662 **and** 3705 |
| `services/invitation_service.py::accept_invitation` | 384 **and** 419 |
| `services/user_merge_service.py::merge_users` | 184 |

Writing eight entries produces two spurious MISSING failures against a correct
implementation.

⚠⚠ **The model test does NOT collect call keywords.** Its `_find_write_sites`
(`test_sessions_invalidated_at_allowlist.py:204-224`) matches `ast.Assign` with
`ast.Attribute` targets only. Copied "function-for-function" as written,
`routers/auth.py::register` — whose ONLY write is the `User(...)` keyword —
reports as a spurious MISSING. The keyword collection is an extension this fence
requires and the model lacks; build it deliberately.

Three further constraints, or it misfires the other way: key
`User(...)` keyword matches on the **callee name**, because `_user_response`
passes `email_verified=` to `UserResponse(...)` (`auth.py:135`, `users.py:62`);
exclude `ast.AnnAssign` (`models/user.py:62`, `schemas/auth.py:59` and
`schemas/admin_orgs.py:35` are annotations, not writes — harmless while the
collector is Attribute-only, load-bearing the moment it is broadened to `Name`
targets); and **declare the tree walked as `backend/app/` only**
— `backend/seed.py:426` writes the column as raw SQL, which no attribute-store
collector can see.
This is what DoD item 2 actually wanted: it demanded parity with the
public-route allowlist, and that allowlist is enforced by a test, not a review
note.

**F12 `test_public_route_allowlist` stays at 26 pairs** — a `PUBLIC_ROUTES` diff
in this PR is the tell that someone wired the endpoint without auth.

**F13 frontend** — the card is absent when `hasPlatformPermission` is false;
submit disabled until the normalized values match and reason ≥ 4; the modal
copy contains the "does not verify the account" sentence (*kills* a silent
re-word that turns the ruling back into an operator-asserts-proof design in the
operator's head). ⚠ Query via `getByRole(role, {name})`, never
`getByLabelText`, which also matches the wrapping `<label>`.

**F14 `test_the_cancel_control_exists_and_calls_the_endpoint`** — with a live
`pending_email`, the card renders a cancel control and clicking it calls
`DELETE /admin/users/{id}/pending-email`. *Kills* shipping the endpoint with no
caller, which is the state the spec's own UI section was originally in.

**F15 `test_the_modal_traps_focus_and_restores_it`** — Tab from the last
focusable element returns to the first, Escape closes, and focus returns to the
trigger. *Kills* a new modal that omits `useFocusTrap`. ⚠ The three existing
field-modals all DO import it; the one that hand-rolls Tab/Escape/restore is
`ConfirmModal`. What is missing is a shared field-modal *component*, not the
hook — so the risk is a fourth bespoke modal that forgets to wire it, which is
what this fence catches.

## UI

`frontend/app/admin/users/[user_id]/page.tsx`, where the operator already reads
`Email verified` via `<YesNo>`.

* ⚠ **The API does not return `pending_email` on the admin payload yet.**
  `admin_users_search_service._serialize_user_row` (`:83-105`) carries
  `email_verified` and not `pending_email`, and no admin schema exposes it. That
  serializer is the **shared list-and-detail** payload, so adding the field also
  exposes pending claims on `GET /admin/users` — intended, but state it. This is
  a third shipped change alongside the two endpoints.
* A `Pending email` row in the identity card, directly under "Email verified"
  so the two read as one story. A labelled `<dt>/<dd>` pair, never a bare
  "Unverified" chip beside an address — a badge that does not name its subject
  reads as a contradiction.
* A new **Account recovery** card, rendered only when the target is unverified
  and the actor holds the permission. **Not** inside the existing `Danger zone`:
  correcting a typo and hard-deleting a user are different intents, and folding
  a routine support action into the red-bordered zone trains operators to ignore
  the red border.
* A new modal. `ConfirmModal` takes `message: string` only — no `children`, no
  field slots — and there is **no shared field-modal abstraction** in the repo:
  `ChangePlanModal`, `FeatureOverrideEditModal` and `BatchDeleteModal` each
  reimplement the same recipe independently. Follow that recipe rather than
  inventing a fourth variant: `<form role="dialog">` plus `useFocusTrap`
  (`frontend/lib/hooks/use-focus-trap.ts`) plus `card`/`input`/`label`/
  `btnPrimary`/`btnSecondary` from `lib/styles.ts`. Contents: current address
  and verified state read-only, new email, confirm, required reason.
* ⚠ **The cancel endpoint needs a control, or it ships with no caller.** When
  `pending_email` is live the card shows it with a **Cancel** action calling
  `DELETE /admin/users/{id}/pending-email`, and a **Resend** that re-runs the
  same POST (no new endpoint — `email_unchanged` compares against
  `target.email`, not `pending_email`). The whole justification for that
  endpoint is an operator recovering from their own mistyped correction, which
  is only reachable from the screen where they made it. Fenced by F14.
* ⚠ **Accessibility is mandated, not left to the implementer.** Focus trap,
  initial focus, Escape to close, and focus restored to the trigger — parity
  with `ConfirmModal`. Fenced by F15. Error banner carries `role="alert"`
  (`page.tsx:356-360` already does; `ChangePlanModal` does not — follow the
  page, not the modal). Buttons stack `flex-col-reverse sm:flex-row` with
  `min-h-[44px]` touch targets, matching `ConfirmModal`/`BatchDeleteModal`.
* **Errors render inline in the modal and the modal stays open.** ⚠ Two
  precedents on this page conflict: `page.tsx:108-114` deliberately closes the
  modal first on delete failure so the page-level banner is not hidden, while
  `ChangePlanModal` keeps its modal open with an inline error. Inline is right
  here because every failure is correctable in place (a mistyped address, a
  short reason, a 409 the operator can act on), unlike a delete whose failure
  ends the interaction.
* **Mirror every backend precondition client-side**, as the Danger Zone card
  already does via `deleteBlockedReason` (`page.tsx:90-98`): the card is
  actionable only for a target that is unverified, active and not a superadmin.
  The server stays authoritative; this stops an operator filling out the form
  to earn a guaranteed 409.
* **Pair the disabled submit with a visible reason**, not a `title` alone —
  `page.tsx:364-379` sets that convention, because a tooltip is not reliably
  accessible.
* Unspecified-state defaults, so they are not invented twice: `—` for an empty
  `pending_email` (the page's convention at `page.tsx:204`), the modal closes on
  success and the identity card revalidates without a full reload, and the
  submit shows a pending state for the duration of the POST.
* **The copy is the ruling, rendered:** *"This does not verify the account. A
  confirmation link is sent to the new address; the account stays locked out
  until the user opens it."* ⚠ It goes on the **card**, not only in the modal.
  The modal is seen only after the operator has already committed to acting,
  which is too late for a sentence whose job is to correct the misunderstanding
  *before* they click. Both surfaces carry it; the shared string lives in one
  place and is not re-worded per surface. Plus a line naming the Google-SSO
  consequence:
  changing the address changes which Google identity can sign in
  (`auth.py:3843` matches on `users.email`).
* Primitives from `frontend/lib/styles.ts`; no raw Tailwind palette colours
  (`check-design-tokens.sh` is a CI gate).
* ⚠ **New card and new modal is new design. Hard pause for operator visual
  approval BEFORE the PR opens.**

## Subtracted from the DoD

1. **"and/or mark it verified"** — struck. It is an unresolved product fork
   wearing the clothes of a detail; two of its three readings ship an
   unproven-verification writer.
2. **"This is an eighth `email_verified` writer"** — struck as a build
   instruction, kept as the security frame. The design adds zero. Replaced by
   fence F11.
3. **Per-actor rate limiting** — not implementable against the single
   `Limiter(key_func=get_client_ip)`. `shared_limit("10/hour", scope=...)` on the existing IP key bounds the
   route IN AGGREGATE and fails **closed** when two operators share an IP.
   ⚠ A plain `limit` does NOT: slowapi buckets it on the concrete
   `request.url.path`, so `{user_id}` gives every target its own private
   budget. That shipped, and was fixed post-merge.
4. **The L4.4 reset-spike Redis fanout** — peer detection across a family of
   three reset endpoints; one endpoint does not make a spike.
5. **Password reset and MFA reset** — out of scope. The shared
   `users.reset_credentials` permission must not drag the whole slice in.
6. **Any change to `resend_verification_public`** — an explicit non-goal. It is
   the seductive small fix and someone will propose it in review. It is
   unauthenticated and username-addressable; letting a caller choose the
   destination mails a credential to a caller-chosen inbox, and under TBD-361
   that token **moves `users.email`**. That is remote account takeover.
7. **Any migration.** `pending_email` already exists; the provenance rides in
   the token.

## Where the ticket's brief is wrong

1. **"The current writer set is closed at seven"** — it is **eight**. The
   ticket's own list enumerates eight sites and calls it seven, because TBD-361
   split the single `verify_email` write into a bootstrap arm and a promotion
   arm. Any fence written against "seven" is wrong on arrival.
2. **"This is an eighth writer and must be reviewed as a security change"** —
   the premise is right, the conclusion does not follow. The correct design adds
   **zero** writers. The framing is an artefact of the ticket being written
   2026-08-09, before TBD-361 shipped.
3. **Every line number in the description is stale.**
4. **"bounced or typo'd" conflates two incidents.** A **typo'd** address needs
   correction. A **bounced-but-correct** address needs delivery, which
   `resend_verification` already handles. Only the typo case needs new code,
   which halves the ticket.
5. **The DoD is silent on the target being another superadmin** — the
   operator-to-operator lateral-movement path, and the highest-severity hole in
   the ticket as written.
6. **The DoD is silent on Google SSO.**
7. **DoD 3 says "must write to `audit_events`" but not "on failure"** — and here
   the refusals are the interesting rows.
8. **"Fences with all three legs" is unfalsifiable as written.** Three legs of
   what? Only the org-admin control is named.
9. **The "known limitation" paragraph reads as if an operator-write would help
   the single-user self-hosted topology.** It would not: it needs a second
   superadmin to press the button exactly as much as this design does.
10. **The ticket assumes the L4.4 spec can be reused.** See the top of this
    document.

## Filed separately, not fixed here

* **TBD-439** — `POST /api/v1/security/csp-report` writes up to 1200 anonymous
  `audit_events` rows/min/IP and the `/admin/audit` exclusion its own docstring
  mandates does not exist.
* **The bootstrap first user** (`auth.py:390`) is verified with **zero inbox
  proof** and is refused by the settled `target_is_superadmin` guard under every
  variant considered. On a self-hosted install that is the likeliest dead-inbox
  victim on the box, and this endpoint will never help them. The remedy is a
  console-side out-of-band action, not a wider endpoint.

## Residual risk — the operator's one bounded veto

> A verified user who has lost **both** their recovery inbox and their
> credential has no in-app remedy and must be recovered by a deliberate, logged
> write against the production database.

Both architects agree this design needs no go/no-go from the operator, because
it introduces no takeover primitive: its accepted population holds no data —
**measured**, not proven, per the precondition recorded above. The wider variants — which do ship the platform's first cross-tenant
takeover primitive — would have needed one.
