---
name: bot-signup-wave-captcha-gate-tomorrow-first-thing
description: "2026-05-20 EOD — bot wave hitting /register with garbage usernames (e.g. \"XzYtsQPnOBeMfkdTQYV\"), never verifying. 403/445 emails bouncing (~90% bounce rate). Op manually deleting orgs. First-thing-tomorrow: add CAPTCHA to registration."
---

# Bot signup wave — CAPTCHA gate (P0 for 2026-05-21)

## Symptom (as of 2026-05-20 EOD)

- Started ~3 days ago (2026-05-17 ish — confirm with `audit_events` / `users.created_at` timestamps when investigating).
- New users registering with garbage random-string usernames: `"XzYtsQPnOBeMfkdTQYV"` is the operator's example. Pattern: ~20-char alnum, no human structure.
- Email verification step never completed by these accounts → orphan unverified users + orgs.
- Provider bounce rate: **403 bounced / 445 sent ≈ 90.6%** in the affected window. Most "users" supplied fake emails too.
- Operator has been manually deleting these orgs as they appear. Not sustainable.

## Working hypothesis

Botnet probing the public `/api/v1/auth/register` endpoint. Pre-launch app, low real signup volume, so the spike is conspicuous against the baseline. Possible motives: building a list of valid-looking accounts for later abuse, recon for a credential-stuffing pivot, or generic spam-relay testing. Operator's read: "likely attempts to invade the system."

## Required defense (build first thing 2026-05-21)

**Add a CAPTCHA to the registration flow.** No further design discussion needed before tomorrow — this is the gate. Candidate primitives (operator picks at brainstorm time):

- **hCaptcha** — privacy-leaning, free tier sufficient, easy to wire to FastAPI as a server-side verify call against `https://api.hcaptcha.com/siteverify`. Frontend renders the widget.
- **Cloudflare Turnstile** — invisible-by-default, lower UX friction, requires Cloudflare account but no domain on CF needed for the widget itself. Server-side verify against `https://challenges.cloudflare.com/turnstile/v0/siteverify`.
- **Google reCAPTCHA v3** — invisible/score-based, but operator has been steering away from Google deps in this app. Likely not the pick.

Operator's preference TBD at brainstorm. Default recommendation: Turnstile for the invisible UX, with a server-side verify in `auth.py` register handler.

## Implementation sketch (do not start until operator confirms direction)

1. Add `CAPTCHA_PROVIDER`, `CAPTCHA_SITE_KEY`, `CAPTCHA_SECRET` env vars (frontend + backend respectively).
2. Frontend `/register` form renders the widget; on submit, POST the token along with the registration payload.
3. Backend `register` handler verifies the token server-side BEFORE creating the user / sending the verification email. Reject with 400 + structured error code `captcha_failed` if verify returns false.
4. Stage rollout: enable in production first (where the bots live), keep dev/staging unaffected or use a test site-key. Operator already prefers env-var gating; this fits.
5. Audit-event emission on failed CAPTCHA so the bounce signature has a downstream trail.

## Out of scope for the CAPTCHA PR

- Rate limiting beyond what slowapi already provides on `/register`.
- Email-provider reputation work (the 90% bounce rate WILL hurt deliverability with the email provider — separate cleanup needed: bulk-delete the orphan users + orgs to bring bounce rate down).
- WAF/Cloudflare-level blocking.

## Companion cleanup (also tomorrow, after CAPTCHA lands)

- Script or admin action to bulk-delete unverified users + orgs older than N hours (operator was doing this manually).
- Decide on the N: 24h is conservative, 1h is aggressive. 24h is probably right because real users sometimes verify slowly.
- The bounce-rate damage to the email provider domain is the second-biggest concern after the bot wave itself. Bulk cleanup helps but may not be sufficient if the provider has already throttled.

## Cross-references

- [[reference_bot_signup_signature]] — diagnostic pattern (username shape + bounce rate + time window) for matching new instances.
- [[feedback_no_em_dashes]] — applies to any user-facing CAPTCHA error copy.
- The slowapi sync-Redis event-loop block concern in [[project_auth_stability_residuals]] is separately latent; CAPTCHA doesn't fix it but slowapi WILL be one of the layers the CAPTCHA verify lives behind.
