# PR 2 — Planned reauth break

PR 2 of the backend-session-model rollout
(`specs/2026-05-17-backend-session-model.md` §8) changes the shape of
the refresh JWT: every newly issued refresh token now carries
mandatory `jti` (per-session id) and `sid` (per-family id) claims AND
a paired Redis row (`auth:session:{jti}` + `auth:session:by_sid:{sid}`).
The validation chain rejects any refresh JWT that lacks either claim
or whose Redis primary key is missing.

This is the operator-facing note for the resulting one-shot reauth
wave.

## What breaks

Every refresh JWT issued **before** PR 2 deploys lacks `jti` and `sid`.
The new validation chain rejects those tokens with HTTP 401 and the
detail `"Session has been invalidated"`.

End-user impact:

1. The next `/api/v1/auth/refresh` (or `/api/v1/auth/verify`) request
   returns 401.
2. The frontend's terminal-vs-transient classifier (PR #287) marks 401
   with that detail string as terminal and routes the user to `/login`.
3. The user re-enters credentials, completes any MFA step, receives a
   new refresh JWT with `jti` + `sid`, and continues.

The user-visible effect is one extra login.

## When it happens

At the moment PR 2 lands on production (DO App Platform deploy from
`main`). The break is instant for any session whose refresh JWT was
issued before the deploy timestamp; pre-existing access tokens remain
valid until their 15-minute TTL expires, at which point the next API
call triggers the `/refresh` attempt that surfaces the 401.

Worst-case-experienced delay between deploy and the user seeing the
login prompt: ~15 minutes (one access-token TTL).

## Why it is acceptable

Operator decision Q7 in
`specs/2026-05-17-backend-session-model.md` §11: pre-launch, before
any external user holds a session, a single forced reauth wave for
QA / dev / SA testers is preferable to carrying a backcompat shim
that would (a) keep the old non-`jti` validation path alive
indefinitely, (b) block PR 3's family-set logic from assuming every
session has a `sid`, and (c) double the surface for the per-session
logout work in PR 4.

The spec is explicit: **no backcompat, no migration, no shims**.
Anyone signed in at deploy time gets exactly one 401 and signs back
in.

## Operator-facing message (if anyone asks)

> We deployed an improvement to how sessions are tracked. As a
> one-time consequence anyone who was signed in just before the
> deploy will be asked to sign in again on their next page action.
> No data is lost. No support action is required — users simply
> sign in and continue.

Tag: `auth.session.reauth-wave.2026-05-17` (use this in support
threads so we can correlate any "had to sign in twice" reports).

## What does NOT break

- Password reset emails issued before the deploy.
- Email verification links issued before the deploy.
- Invitation links (different JWT type with its own claims).
- MFA challenge tokens mid-flow (5-minute TTL; completes normally
  and issues a PR 2-shaped refresh JWT).
- Any DB row, audit event, or org configuration.

## Reference

- Spec §11 Q7 — operator decision recording the acceptance.
- Spec §8 PR 2 — scope of the JWT shape change.
- Spec §3.1 — new claim shape with `jti` and `sid`.
- Spec §4 — Redis schema for `auth:session:{jti}` and
  `auth:session:by_sid:{sid}`.
