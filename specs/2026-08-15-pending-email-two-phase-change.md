# Two-phase email change (`pending_email`) — TBD-361

**Status:** design ruled, 2026-08-15. Two independent architects plus one
concede-or-defend round. Binding.

## The defect

`PUT /api/v1/users/me` (the ticket says PATCH; there is no PATCH route) does
three things in one request when the email changes:

- `current_user.email = new_email` — immediately (`routers/users.py:184`)
- `current_user.email_verified = False` (`:187`)
- `current_user.sessions_invalidated_at = now` (`:192`)

then mails a verification token to the **new** address. `POST /auth/login`
403s unverified users unconditionally (`auth.py:511-518`).

So one typo logs the user out and locks them out. Every recovery path mails
`user.email`, which is now the typo: `forgot_password` (`auth.py:1888-1902`)
sends there, and `reset_password` (`:1939-1949`) writes only password fields
and never `email_verified`, so even a successful reset still 403s.

## What the ticket got wrong

**"No recovery path in any environment" is false.** In a multi-member org:
another admin invites the user's real address (`invitation_service.py:411-420`
creates a new verified row), a superadmin merges it into the locked row
(`user_merge_service.py:183-184` sets `target.email_verified = True`), and the
user logs in with their **username** (`auth.py:492-495` accepts username or
email).

**This strengthens the ruling rather than weakening it.** That path needs two
humans, one a superadmin, and it ends by deleting the source row
(`user_merge_service.py:189`). It is incident response, not recovery. And it
does not exist at all for a **solo org**, which is the default: every
`POST /auth/register` mints its own org (`auth.py:355`), invitations are
hard-scoped to `current_user.org_id` (`org_members.py:92`), `admin_users.py`
has no invite route, and the org member-patch writes only `role`/`is_active`
while refusing the last active owner.

Also wrong: the ticket implies no validation, but a 409 uniqueness check
exists at `users.py:167-174`. Google SSO is not an escape — the lookup is
`User.email == email` at `auth.py:3357` and there is **no `google_sub`
column**.

## Ruling: prevention, not recovery

A `pending_email` column. The verified address and the live session survive
until the **new** address confirms. An email change becomes a two-phase
commit rather than a destructive write.

**No operator break-glass, and none filed.** A superadmin route that rewrites
`users.email` + `email_verified` is a full account-takeover primitive, and on
a solo self-hosted install the locked-out user *is* the only superadmin — so
it would help only the population that already has the merge path. After this
change nobody can write a typo into the live column, so the residual is
historical rows: a one-off manual fix, not a feature.

## Schema

```
users.pending_email  VARCHAR(120)  NULL
```

**No unique index, no index at all.** `pending_email` is an *unproven claim*.
A unique constraint enforces the wrong invariant — it does not stop user A
claiming an address that equals user B's live `users.email`, which is the
collision that matters — and it creates an **address-squatting denial
primitive**: any authenticated account could claim an address, never prove
it, and block its legitimate owner from even requesting the change. Two users
may both claim an address; only one can prove it. First to click wins.

**No `pending_email_requested_at`.** Ruled out in round 3, conceded by its
proposer. `audit_events.created_at` already records when each request was
made, durably, for **every** request including superseded ones — a column on
`users` would record only the latest, which is strictly worse evidence.
Nothing in the write path, the promote path, or cancel branches on it. Rate
limiting is slowapi/IP-keyed (`users.py:82`, `auth.py:2051`) and reads no
column. A nullable column with zero readers is indistinguishable from one
whose reader was deleted, and the next reader will assume it gates something.

Migration `080_pending_email.py`, `revision = "080_pending_email"`,
`down_revision = "079_audit_api_token_id"` (current head).

## Request path — `PUT /users/me`

Record the claim; change nothing about identity.

- Normalize with `user_service.normalize_email` at **three** sites, not two:
  the `email_changing` comparison (`users.py:117-119`), the uniqueness
  pre-check (`:167`), and the write (`:184`). Missing the first makes a
  pure case change (`Foo@Bar.com`) read as a change and start a whole
  pending flow for the same address. Missing the others stores a raw value
  that a normalizing lookup — `forgot_password` (`auth.py:1895`), the Google
  callback (`:3326`) — can no longer find once promoted, and reopens the
  TBD-322 case-collision class.
- Keep the 409 uniqueness pre-check against `users.email`, now normalized. It
  is advisory: a courtesy so the user does not wait for a link that cannot
  work.
- Keep the existing re-auth gate (`users.py:136-162`) unchanged. It closes
  S-P1-2: without it a session-only compromise could swap the recovery
  channel.
- Write `pending_email` only. **Do not** touch `email`, `email_verified`, or
  `sessions_invalidated_at`.
- ⚠ **Submitting the CURRENT address while a claim is live CLEARS the claim.**
  Without this, `email_changing` is False (both sides normalized), the claim
  survives, and the natural undo gesture silently does nothing — leaving a
  state the user cannot leave through the profile form, with the mistyped
  address still clickable for 24h. A stranger who owns that address clicks,
  promotes, and then owns an account whose `forgot_password` mails *their*
  inbox. So: when `body.email` normalizes to `current_user.email` and
  `pending_email is not None`, clear it (no re-auth — clearing only restores
  the status quo).
  ⚠ This is a **backend** safety net for API clients. It does **not** give the
  UI an escape: `frontend/app/settings/page.tsx:78-88` re-seeds the field from
  `user.email` on every `refreshMe()`, and `:131-137` omits `email` from the
  payload whenever it equals `user.email`, so the browser never transmits it.
  The UI escape is the Cancel affordance, which is therefore a **hard
  co-requisite of this ticket, not an optional follow-up** — see §Frontend.
- Mint `create_email_verification_token(user.id, pending_email)` and mail
  **the pending address**.

## Promote path — `POST /auth/verify-email`

Replace the S-P2-1 guard at `auth.py:2038-2039` with:

```python
token_email = payload.get("email")
if not token_email:
    raise HTTPException(400, "Invalid or expired verification token")
promoting = (
    user.pending_email is not None
    and token_email == user.pending_email
)
if not promoting and token_email != user.email:
    raise HTTPException(400, "Invalid or expired verification token")
```

Compare **exactly**; do not casefold at compare time. Normalize at the write,
compare exactly at the read — casefolding here would accept a token whose
claim differs from the stored value, weakening S-P2-1 rather than hardening
it.

**A token minted for the CURRENT address must still verify.** Three live
callers mint from `user.email` with no pending row: `register`
(`auth.py:408`), `resend_verification` (`:2061`), and
`resend_verification_public` (`:2088`). Dropping that arm breaks every
first-time verification, and **no `pending_email` test would catch it**,
because `pending_email` is NULL in all of them.

What each arm refuses:

- **current-address arm** — the original S-P2-1 replay: a link mailed to an
  address the user has since moved away from must not launder that address
  into `email_verified = True`.
- **pending arm, pinned to the live column** — replay of a *superseded or
  cancelled* claim. Claim `b@x`, change to `c@x`, and the `b@x` link is inert
  with no revocation list needed.
- **`not token_email`** — pre-S-P2-1 tokens carrying no claim, which would
  verify whatever the row currently holds.
- **together with `sub`** — cross-account promotion. `sub` pins the user,
  `email` pins the address; neither alone does.

On the promoting branch, in one commit:

1. Re-check uniqueness: `select(User).where(User.email == pending, User.id != user.id)`.
   Another row owns it → clear `pending_email`, commit the clear, return
   **409** with an actionable message. Not 400 — 400 is reserved for "bad
   token" and must stay uninformative; the token is valid, the world changed.
   Clearing lets the user request again.
   ⚠ **This SELECT is collation-dependent.** `users.email` is pinned to
   `utf8mb4_0900_ai_ci` (`alembic/versions/040_users_email_case_insensitive.py`);
   SQLite — every CI shard — compares binary. Mixed-case `users.email` rows
   genuinely exist in production, because today's `users.py:184` writes
   `body.email` raw. So for a legacy row `Foo@Bar.com` versus a claim
   `foo@bar.com`, MySQL matches and 409s (correct) while SQLite misses **and**
   its UNIQUE index misses, so the IntegrityError backstop never fires and two
   rows land at the same address differing only in case — the TBD-322 /
   migration-040 collision class. An implementation that omits this SELECT is
   green on every shard. Fence 9 therefore uses an **exact-case** collision so
   it is testable on the shards; the mixed-case leg is accepted residual
   covered by migration 040 and stated here rather than left implicit.
2. Wrap the commit in `except IntegrityError` → rollback, clear
   `pending_email`, 409. ⚠ Only *loosely* the `auth.py:392-399` pattern: that
   one rolls back and raises, and never writes afterwards. Here the rollback
   **expires the `user` instance**, so the clear needs an explicit re-read
   before a second commit — and that second commit can itself fail, so it
   needs its own guard. **This is now the only
   reachable site for that violation** — the request path no longer writes
   `users.email` — and an uncaught one would be a **500 on a link click**, at
   the exact moment the user believes their account is being repaired.
3. Refuse if `not user.is_active`, **promotion branch only** (generic 400), so
   a suspended account cannot rotate its recovery address mid-investigation.
   Do not add this to the bootstrap arm; that changes existing behaviour for
   a case with no defect.
4. `user.email = pending`, `pending_email = None`, `email_verified = True`.
5. `sessions_invalidated_at = datetime.now(timezone.utc)` — **NOT floored**.

**The cutoff moves to promotion, and only promotion.** Identity changes at
confirm, not at request. The write must sit inside `if promoting:` — a
blanket write would log out every bootstrap verification.

**Do NOT floor it.** ⚠ Both architects independently ruled "floor it with
`.replace(microsecond=0)`", reasoning from the fsp-0 rounding note at
`invitation_service.py:370-381`. **That ruling is overturned: flooring is
actively wrong here, and it is the only variant that leaves a session alive
across an identity change.**

The mechanism. All three validators compare with a **strict `<`**
(`deps.py:66`, `deps.py:124`, `auth.py:1076`), and `create_access_token`
already stamps `iat = int(now.timestamp())` — floored to the whole second
(`security.py:53-64`). So with promotion at wall-clock `T.9`:

| cutoff written | stored | token minted `T.2` (`iat = T`) | outcome |
|---|---|---|---|
| floored `T.0` | `T` | `T < T` → False | **survives — wrong** |
| unfloored `T.9` | MySQL rounds to `T+1`; SQLite keeps `T.9` | `T < T+1` → True | rejected — correct |

It does not stay sub-second: `POST /auth/refresh` uses the same strict `<`, so
a refresh token landing in that second survives, mints a fresh access token
with a later `iat`, and the session becomes **permanently renewable past an
identity change**.

The flooring rationale was also prospective — it conceded `verify_email` mints
nothing today — so it paid a live weakening for a benefit that does not exist.

Use plain `datetime.now(timezone.utc)`, matching `reset_password`
(`auth.py:1947-1948`) and today's `update_profile` (`users.py:192`). If
`verify_email` ever mints a session, adopt `invitation_service`'s pattern
**wholesale**: its own comment says *both* columns must be floored, because
`token_cutoff` maxes two — flooring one is precisely the shape it warns about.

⚠ This is why fence 3 must exist and must be run against the correct
implementation: under the floored variant it goes RED, and the tempting
repair is to weaken the fence rather than the code.

## Audit and notifications — split the event

`users.py:218-311` currently fires, at request time, an audit row plus an
in-app notification plus **two** security emails: an alert to the old address
and a confirmation to the new one. Under this design **nothing has changed
yet at that moment**.

- **Request time** → `user.email.change_requested`, carrying the old-address
  **alert only**. That alert is the anti-hijack channel and genuinely belongs
  here.
- **Promotion time** → `user.email.changed`, with `detail.new_email` sourced
  from the **promoted** value, not `current_user.email`.

⚠ `users.py:240` currently reads `detail["new_email"] = current_user.email`.
Left alone, that column no longer changes and the row asserts the email
changed to itself. The *tempting* fix — source it from the pending value but
leave the event at request time — is green and still wrong: it writes a
completed-change record for a change that may never happen, and mails the old
address "your email was changed to X" when it was not.

## Cancel — `DELETE /api/v1/users/me/pending-email`

Authenticated, `require_interactive_session`, **no password or step-up**,
idempotent 204, and rate-limited like every neighbour in `users.py`. Writes
`None`, never `""`. Cancelling only restores the status quo; it cannot move the
recovery channel, and requiring re-auth to undo a mistake is the exact trap
that created this ticket. Without it the only escape from a wrong pending
address is submitting another one, which re-demands the password.

## Lifecycle

| Event | Behaviour |
|---|---|
| Second change while pending | **Last write wins**, unconditionally. No "one at a time" 409 — that strands the double-typo user, who is this ticket's user. The old token dies via the pending arm. |
| Cancel | Clears `pending_email`. |
| Token expires (24h) | **Nothing.** The token's `exp` is the clock. A stale claim is inert: every mint site reads `user.email`, so nothing can resurrect it. **No sweeper job.** |
| `forgot_password` mid-pending | **Unchanged — this is the deliverable.** The current address is still live and verified, so the link reaches a mailbox the user controls. ⚠ It must **not** also match `pending_email`: mailing a reset token to an unverified self-asserted address is account takeover. |
| `reset_password` mid-pending | Must **not** clear `pending_email`. A password reset makes no statement about the email claim. |
| Admin deactivates mid-pending | Columns untouched; promotion refuses (above). |

`pending_email` is cleared by exactly four writers: promotion, explicit
cancel, overwrite by a later request, and promote-time conflict abort.
Nothing else. Record that list in a docstring on the column — the discipline
`linked_transaction_id` needed and did not have.

## Residuals, stated not fixed

**PATs survive promotion.** `auth/pat.py:177` deliberately applies no
`token_cutoff`, so "an identity change kills every session" is false for
PAT-authenticated access. Consistent with documented PAT design; stated here
rather than left implied.

**`register` can squat a live claim.** `auth.py:340-348` 409s only against
`users.email`, so anyone can register, unauthenticated, at an address that is
someone's live `pending_email`; the claimant's promotion then 409s forever.
Not a regression — today the same squat 409s at request time instead — but
the §Schema rationale ("a unique index would create a squatting primitive")
must not be read as claiming the design removes squatting altogether. It
removes it from `pending_email`, not from `register`. The promote-time 409
copy must therefore not imply user error.

**Google SSO after promotion.** `auth.py:3357` matches on `User.email` and
the step-up at `:3901` compares `google_email` to `user.email`. A
`password_set=False` user who promotes to a non-Google address loses Google
sign-in — a fresh login mints a new user and org — and loses their only
step-up path. Pre-existing, but this design makes email changes *safe* and
therefore more frequent.

**`pending_email == user.email` is unreachable**, so no defensive branch is
needed: `email_changing` normalizes both sides, and no other writer touches
`users.email` while a claim is live (`user_merge_service.py:183-184` writes
only `email_verified`; `admin_users.py` has no email-edit route). If it ever
occurred, the promoting arm fires and costs one spurious global logout.

## Known limitation, filed not fixed

`resend_verification` (`auth.py:2052-2062`) early-returns "Email already
verified" and mints against `user.email`. A pending user stays
`email_verified = True`, so **there is no resend path for the pending
address**; re-submitting the profile form is the workaround, and it demands
the password again. Both architects independently ruled that endpoint stays
bound to `user.email` — it is the *verification* resend, not the *change*
resend — so this is a consequence of the ruling, not a defect in it. Filed.

## Fences

Every one names the wrong implementation it kills and must be proven RED
against it.

1. **The recipient fence.** Read the args queued on
   `background_tasks.add_task(send_verification_email, …)`; assert the
   recipient is the **new** address; feed **that exact token** into
   `/auth/verify-email`; assert `user.email` is now the new address and
   `pending_email is None`.
   ⚠ **The input address MUST be mixed-case**, and the fence must ALSO assert
   the queued token's `email` claim equals the stored `pending_email`
   byte-for-byte. With an all-lowercase fixture both readings are identical
   and the fence proves nothing about which value was minted from. The failure
   it must catch: mint from `body.email = "Foo@Bar.com"` while storing
   `pending_email = "foo@bar.com"`. The user clicks their own link,
   `token_email == user.pending_email` is False (exact compare, by design),
   it falls through to the current-address arm, and the link **400s
   permanently** — with no resend path, behind a 5/hour IP-keyed limit.
   Patch `app.routers.users.send_verification_email` (module-scope import at
   `users.py:26`, resolved by `add_task` at call time), not
   `app.services.email_service`; the shape already used at
   `tests/routers/test_rate_limit_sensitive_endpoints.py:268`. Background
   tasks do run under the test client. *Kills:* minting against `user.email` (one
   careless copy of the `resend_verification` shape). The mail then lands in
   the **old** inbox, the guard waves it through on the current-address arm as
   an ordinary verification, the change silently never happens — and every
   hand-minted unit test stays green, because nothing else compares the
   address mailed against the address typed.
2. **Session survives the request.** After a change request, the caller's
   existing access token still authenticates. *Kills:* the request-time
   cutoff write returning.
3. **Pre-change token dies at promotion.** A token minted before the change is
   rejected after promotion. *Kills:* dropping the cutoff entirely — which
   fence 2 alone would reward.
4. **Bootstrap is untouched.** A bootstrap verification (pending NULL) leaves
   `sessions_invalidated_at` unchanged; a promotion sets it. *Kills:* the
   cutoff written outside `if promoting:` — which the AST allowlist
   structurally **cannot** see, being function-granular.
5. **Login with the old address still works while pending.**
6. **`forgot_password` mid-pending mails the old, still-live address.**
   *Kills:* widening its lookup to `pending_email` (account takeover).
7. **Second change supersedes.** The first token now 400s. ⚠ The second
   claim must be a **different** address: re-claiming the same one leaves the
   first token legitimately valid, so the fence would pass for the wrong
   reason.
8. **Cancel clears**, and the token then 400s. Assert `pending_email IS
   NULL`, not falsy: `""` passes `is not None`, so the guard still falls
   through safely, but `UserResponse` would serialize `""` and render an empty
   pending row.
9. **Promote-time collision → 409**, `pending_email` cleared, not a 500. Use
   an **exact-case** collision so it runs on the SQLite shards (see the
   collation note in §Promote path).
10. **Case-only change is a no-op** — and, when a claim is live, **clears
    it**. Seed the stored row **raw and mixed-case**, because that is the
    population production actually holds (`users.py:184` writes raw today); a
    fence seeded from an already-normalized row does not test it.
    *Kills:* the missing `normalize_email` at `users.py:117-119`, and a bare
    no-op that leaves the claim stranded (§Request path).
11. **Deactivated user cannot promote.**
12. **Audit split:** no `user.email.changed` at request time; a
    `change_requested` instead; `changed` at promotion with the correct
    `new_email`.
13. **`GET /auth/me` carries `pending_email`.** *Kills:* populating only the
    `users.py` builder, which leaves the field `undefined` on every page the
    user actually sees while every other fence stays green.

**Where these live.** Fences 3, 4, 7, 8, 9, 11 extend
`backend/tests/auth/test_verify_email_endpoint.py` (TBD-366, `8adb099b`) —
the direct fence file for the guard being replaced. Its V5/V6 mutate
`users.email` directly, so they still pin the current-address arm unchanged
and are **not** edited; V5's narration ("the user changes their email") is
reworded, since that is no longer how the product does it. Fences 1, 2, 10,
12, 13 belong with the `PUT /users/me` router tests.

### The AST allowlist

`tests/auth/test_sessions_invalidated_at_allowlist.py:86` — **move** the
entry, do not delete it. `("routers/users.py", "update_profile", …)` becomes
`("routers/auth.py", "verify_email", …)` with the relocation reason inline.
The MISSING direction then pins spec §6 trigger 3 at its new home; the
UNEXPECTED direction pins `update_profile` **shut**, which is strictly
stronger than today.

Deleting the entry and stopping leaves the trigger pinned by nothing. Adding
`verify_email` while leaving `update_profile` in place fails the MISSING
assertion, and the tempting "fix" is to delete the assertion — it must be
fixed by removing the entry.

Add a **path pin** mirroring `test_auth_logout_handler_no_longer_writes_cutoff`
(`:259-302`): slice `update_profile`'s body and assert
`".sessions_invalidated_at" not in body_text`. A new helper in `users.py`
called from `update_profile` would otherwise satisfy the function-name
allowlist while restoring the lockout.

## Frontend

Backend-only is **not shippable**: `frontend/app/settings/page.tsx:175-179`
says *"Profile updated. Check your new inbox for a verification link. You'll
need to sign in again."* After this change the profile was not updated,
`refreshMe()` snaps the field back to the old address, and the user is not
signed out. Every clause is false, and the user sees their edit revert with
no explanation — which reads as a failed save.

1. `UserResponse` emits `pending_email`, and ⚠ **there are TWO
   `_user_response` builders — populate both**:
   - `routers/auth.py:126` — serves `GET /auth/me`, `POST /auth/register`,
     `/auth/refresh`.
   - `routers/users.py:53` — serves `/users/me` only.

   `AuthProvider.fetchMe` calls **`/api/v1/auth/me`**
   (`frontend/components/auth/AuthProvider.tsx:207,281`), so the `user` object
   the settings page renders from comes from the **auth.py** builder.
   Populating only `users.py` leaves `user.pending_email` `undefined` on every
   page: the pending row never renders, the rewritten copy has nothing to key
   off after `refreshMe()`, and **every backend fence stays green**. Fence 13
   pins `/auth/me` specifically.

   `frontend/lib/types.ts` carries it. ⚠ Type it **optional** on the TS side
   or update every `User` fixture in the same PR — a required field on a
   shared type breaks CI with no backend gate able to see it.
2. Rewrite the success copy to the truth. **Bugfix**, no gate: it restores a
   statement that this change makes false.
3. A pending-change row: pending address plus Cancel. Reuses the
   `mt-0.5 text-[10px]` sub-line beside "Email verified" (`:230-232`).
   ⚠ **New persistent UI state and a new interaction. Operator visual
   approval required before the PR opens.**
   ⚠ **Hard co-requisite, not optional.** It is the only escape the browser
   can express from a live claim (§Request path): the settings form never
   transmits an unchanged address, so the backend safety net is reachable by
   API clients only. Shipping the backend without this leaves a UI state the
   user cannot leave.
4. `frontend/app/verify-email/page.tsx` — after a **promotion** the user is
   signed out (the cutoff), so the page must say so and route to login.
   Bootstrap verification is unchanged.

## Subtract

1. Unique index on `pending_email` — wrong invariant, plus a squatting
   primitive.
2. `pending_email_requested_at` — no reader; `audit_events.created_at` is
   better evidence.
3. Any scheduled sweeper for expired claims — new failure surface, zero
   behaviour change.
4. An operator break-glass endpoint, and no follow-up ticket for one.
5. Confirm-email-twice. The design makes a typo survivable, so double entry
   taxes every correct user to soften a failure that no longer has
   consequences — and it *fights* the design by teaching "this is
   irreversible", which is now false. Filed as UX if wanted.
6. Any edit to `forgot_password` or `reset_password`. The deliverable is that
   they need none.
7. Flooring the existing `reset_password` cutoff write — no live defect, and
   it drags a second allowlisted site into the diff.
