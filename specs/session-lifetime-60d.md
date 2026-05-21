---
name: session-lifetime-60d
description: "Pending spec — separate refresh-TTL (idle ceiling) from absolute-session-lifetime semantics, allow org override up to 60 days. Surfaced during the 2026-05-16 cookie-shadow incident when fjorge's expected \"60-day session\" turned out not to be configured anywhere."
---

# Session-lifetime semantics + 60d org override (pending spec)

## Trigger

2026-05-16 incident investigation surfaced that fjorge's expected
"60-day session length" is **not configured anywhere** in pfv:

- `backend/app/config.py:25` — `JWT_REFRESH_TOKEN_EXPIRE_DAYS=7` (the
  hard ceiling — both the refresh JWT `exp` and the cookie `max_age`)
- `backend/app/config.py:27` — `SESSION_LIFETIME_DAYS=30` (commented
  in `.env.example`, governs absolute lifetime via
  `_validate_single_refresh_token`'s `session_created_at` check)
- four hardcoded `max_age=7*24*60*60` in `backend/app/routers/auth.py`
  (lines 296, 446, 530 implied, 714, 1363) — drift bait

So the practical ceiling is 7 days idle (or however long the user
keeps actively refreshing), with a 30-day absolute cap. "60 days" is
not a knob anyone has turned.

**Why:** The mismatch hides a real policy question: refresh-TTL
governs idle-out, `session_lifetime_days` governs absolute session
age. Conflating them in env knobs invites further drift.

**How to apply:** Before bumping any session-related constant, finish
this spec so the three concepts (refresh-cookie max_age, refresh-JWT
exp, absolute session lifetime) are explicit and the cookie/JWT/policy
TTLs can never silently drift apart.

## Out of scope for the 2026-05-16 cookie-shadow hotfix (PR #289)

Per architect direction during the incident, this spec does NOT ride
along with PR #289. That hotfix is narrowly the legacy-cookie cleanup
+ duplicate-cookie reader. Session lifetime is its own PR with its
own tests.

## Proposed semantics

1. **Refresh cookie/JWT max_age = idle ceiling.** Caps how long a user
   can be away before they must re-authenticate. Default 30 days.
2. **`session_lifetime_days` = absolute session age.** Refresh
   rotation carries `session_created_at` from original login; once
   `now - session_created_at > session_lifetime_days` the session
   ends regardless of recent activity. Default 30 days.
3. **Org-level override.** `OrgSetting(key="session_lifetime_days")`
   already exists and is honored in `_validate_single_refresh_token`.
   Add validation: `1 <= value <= 90`. Optionally allow override of
   the refresh-cookie max_age separately.
4. **Single source of truth.** Drop the four hardcoded
   `7*24*60*60` literals. Use `app_settings.jwt_refresh_token_expire_days
   * 24 * 60 * 60` everywhere — when the env value changes, every
   set_cookie picks it up.

## Tests the spec PR must add

- Default org gets 30-day absolute lifetime behavior end-to-end.
- Org override to 60 is honored on `/refresh` validation.
- Refresh rotation preserves `session_created_at` across rotations
  (regression already exists; pin it for this work).
- Expired absolute lifetime still logs out even if the refresh JWT
  itself has not expired (current `SESSION_EXPIRED_DETAIL` path).
- Org `session_lifetime_days` outside `[1, 90]` is rejected at the
  setting-write site (admin endpoint or whichever writes
  `OrgSetting`).

## Migration

No data migration. The constants and the validation rule change is
pure code. Cookie max_age change affects future logins only; existing
cookies expire by their currently-set max_age. PR can ship
non-emergent.

## Cross-references

- [[cookie-attribute-migration-trap]] — the related cookie discipline
- PR #211 — the cookie path widening that exposed the cookie-attribute
  drift class
- PR #289 — the legacy cookie cleanup hotfix
- `backend/app/routers/auth.py:296, 446, 714, 1363` — hardcoded
  `max_age` sites to consolidate
- `backend/app/config.py:25-27` — current TTL settings
- `backend/app/models/settings.py` — `OrgSetting` substrate for the
  override

## Status

- **2026-05-16**: spec captured, not started. P2 in launch path.
- **Owner**: TBD.
