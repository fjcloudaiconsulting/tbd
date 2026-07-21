# Superadmin Personal Access Tokens (PAT) — v1 Design Spec

**Status:** Design signed off — architect + security design review (both APPROVE-WITH-RULINGS) and a final spec-level security re-review (APPROVE-WITH-CHANGES), all folded, 2026-07-21. Implementation-ready.
**Scope:** v1 = superadmin-only. Data model deliberately supports a future per-user PAT phase without a rewrite.
**Author context:** Motivated by 15-minute JWT access tokens being unusable for curl/automation (surfaced during the 2026-07-20 outage-apology send).

---

## 1. Goal & non-goals

### Goal
Let a superadmin mint long-lived, scoped, revocable bearer tokens (`Authorization: Bearer pat_<secret>`) so automation and curl can call the API without a 15-minute JWT. Management lives at a dedicated `/system/api-tokens` superadmin page.

### Non-goals (explicitly deferred — see §12 backlog)
- Per-user (customer-facing) PATs.
- Fine-grained per-permission scopes (v1 ships coarse `read`/`write`).
- A positive per-route allowlist of PAT-callable endpoints (v1 uses method-scope + a deny-list of interactive-only endpoints).
- Per-token rate-limit counters (v1 rate-limits only the mint endpoint).
- SalesForce-style "password rotation kills tokens" coupling (v1 uses the GitHub model: PATs are independent of the password).
- Public product documentation (comes with the customer-facing phase).

---

## 2. Threat model & posture (why the design looks the way it does)

A v1 PAT authenticates **as the minting superadmin** and is therefore effectively a **platform master key**: a `write` token that leaks is a cross-org compromise, and even a `read` token exfiltrates **every org's** financial data and PII. The design treats it accordingly:

- **Mandatory short-ish expiry** (30d default, 90d hard max) bounds the live-leak window.
- **Step-up on mint** stops a hijacked-but-live session from silently minting a persistent backdoor.
- **Interactive-session-only carve-outs** (account-takeover surface + all Tier-0 destructive ops) mean a leaked PAT can never escalate into permanent account ownership or irreversible platform damage.
- **Instant revoke + revoke-all + prominent visibility + email-on-mint** are the compensating controls that make "password reset does NOT kill PATs" survivable.
- **Honest framing:** method-based `read`/`write` is a *coarse abuse-limiter, not a data-confidentiality boundary*. `read` still exposes all-org data. This is stated in the UI copy and docs.

---

## 3. Token format & at-rest storage

- **Token string:** `pat_` + `secrets.token_urlsafe(32)` (256-bit entropy). The `pat_` prefix is a stable, publishable secret-scanning signature.
- **At rest:** store `HMAC-SHA256(secret)` (hex) as the **unique-indexed lookup key**. Never store plaintext.
- **Pepper key — dedicated & decoupled (SEC-R1, the P1 landmine):** the HMAC key is a **new config value `API_TOKEN_HMAC_KEY`**, used *directly* (single-purpose, NOT via `derive_hmac_key`, which recomputes from `jwt_secret_key` at call time). A verify-only `API_TOKEN_HMAC_KEY_PREV` supports two-deploy key rotation (mint under new, verify under new-or-prev) so a pepper rotation drains gracefully instead of 401-ing the whole automation fleet — exactly the failure mode the MFA_RECOVERY_HMAC_KEY decouple exists to prevent.
- **Validation — explicit prod gate, NOT the optional MFA validator (SEC re-review Finding 1):** the MFA_RECOVERY_HMAC_KEY validator is fully optional (empty → no-op) and has no prod-required branch; mirroring it verbatim would let an unset `API_TOKEN_HMAC_KEY` silently fall back to the jwt-derived pepper **in production**, reintroducing the exact coupling SEC-R1 forbids. Instead use a `model_validator(mode="after")` that: (a) when a value is present, enforces `len ≥ 32` and `!= jwt_secret_key` (reuse the MFA idiom for these two checks); **and (b) raises when `app_env == "production" and not api_token_hmac_key`** (the established prod idiom `app_settings.app_env == "production"`, cf. `main.py:267`). The `derive_hmac_key` dev fallback is reachable **only** off-prod.
- **Implementer note (SEC re-review Finding 5):** `derive_hmac_key`'s signature is `purpose: bytes` (`security.py:16`), so the dev fallback is `derive_hmac_key(b"api_token")` (returns bytes). The primary path must `.encode()` the configured string: `hmac.new(settings.api_token_hmac_key.encode(), secret.encode(), sha256)`. The two paths intentionally produce different key material; don't miswire the fallback.
- **Hash choice justification (SEC-R8):** a fast deterministic keyed hash is correct here *because* the secret is 256-bit random — there is nothing to slow-hash against, and bcrypt/argon2 would add latency to every API call. The pepper makes a DB-only dump useless (attacker lacks the key). Lookup is by full digest against a unique index; no `secrets.compare_digest` is separately required because the attacker cannot produce a digest without the secret. **Never** fetch-by-prefix-then-compare-in-Python; if that ever changes, use `secrets.compare_digest`.
- **Display:** store `token_prefix` = `pat_` + first 6–8 chars of the secret (identify, not guess). Plaintext is returned in the mint response **once** and never again.

---

## 4. Data model — `api_tokens` table (migration `071_api_tokens`)

`down_revision = "070_broadcast_delivery_status"` (verified current head). Register the model in `backend/app/models/__init__.py` (import + `__all__`).

| Column | Type | Notes |
|---|---|---|
| `id` | BigInteger PK (Integer on SQLite, per codebase variant pattern) | |
| `token_hash` | `String(64)`, **unique index** | HMAC-SHA256 hex; the O(1) lookup key |
| `token_prefix` | `String(16)` | Non-secret display hint |
| `name` | `String(100)`, NOT NULL | Human label ("broadcast cron", "local scripting") |
| `scope` | `String(16)`, NOT NULL | App-validated `"read"` \| `"write"` (`write ⊇ read`). **App-validated String, NOT a MySQL ENUM** (ARC-R6 — the ALTER-ENUM landmine; the axis is designed to grow). |
| `created_by_user_id` | FK → `users.id`, **`ON DELETE SET NULL`**, nullable, indexed | The acting identity. SET NULL matches the forensic-snapshot idiom (ARC-R9); a token whose owner is gone simply fails to authenticate. |
| `created_by_email` | `String(255)`, NOT NULL | Snapshot for display/forensics, survives user deletion |
| `expires_at` | naive-UTC `DateTime`, NOT NULL | Mandatory expiry (ARC-R7) |
| `created_at` | naive-UTC `DateTime`, `server_default=now()` | |
| `revoked_at` | naive-UTC `DateTime`, nullable | Revocation marker |
| `last_used_at` | naive-UTC `DateTime`, nullable | Throttled write (§8) |
| `last_used_ip` | `String(45)`, nullable | Via `get_client_ip` only (never raw `request.client`) |
| `reminder_stage` | `SmallInteger`, default 0 | Dedupe expiry reminders: 0=none, 1=14d sent, 2=3d sent, 3=expiry sent |

**Datetime handling (ARC-R7):** all datetimes are **naive UTC** (`sa.DateTime()`, no `timezone=True`) to match every other column in the codebase. Every expiry/revocation comparison in `authenticate_pat` normalizes via an `_aware()`-style helper (`.replace(tzinfo=timezone.utc)`) before comparing to `datetime.now(timezone.utc)`, or the naive-vs-aware comparison raises `TypeError`.

**No `org_id` on the token.** Org-scoping is inherited by re-reading the acting user's live `org_id` every request (mirrors `get_current_user`, deps.py:41). This is precisely what lets per-user tokens drop in later.

**Org-delete cascade (ARC-R9 / `reference_org_delete_cascade_fk_audit`):** the FK is to `users`, not `organizations`, and is `SET NULL`, so an org wipe (which deletes the org's users) leaves orphaned-owner tokens that simply stop authenticating. Confirm no ordering break in `wipe_org_data` / `reset_org_data`; SET NULL is safe.

**Migration verification (ARC-R8):** run up **and** down on an isolated real-MySQL `-p team-*` stack — SQLite CI green ≠ MySQL DDL proven, and this table needs a `UNIQUE` index (historic MySQL index-length / FK-cover landmines).

---

## 5. Scope model (v1 coarse)

- Single `scope` per token: **`read`** (safe methods) or **`write`** (`write ⊇ read`, so a `write` token may also GET).
- **Enforced centrally in the auth path, fail-closed** (SEC-R3 / ARC-R4): in `authenticate_pat`, if `request.method in {POST, PUT, PATCH, DELETE}` the token must have `scope == "write"`, else `403`; GET/HEAD require `read` or `write`. Any unmapped method → deny. Because *every* route depends on `get_current_user`, enforcement is universal and impossible to forget per-route.
- **Documented caveat (ARC-R4):** a few POST endpoints are semantically reads (e.g. `reports.query`, exports). In v1 they therefore require a `write`-scoped PAT. This is called out in the UI help + docs; a read-safe POST allowlist is deferred to the fine-grained phase.
- **Honest framing (SEC-R3):** method-scope is a coarse abuse-limiter, not a confidentiality boundary. A `read` PAT still reads all orgs.

---

## 6. Authentication path

**Seam (ARC-R3):** keep the branch inside `get_current_user` so PAT identity flows through every existing `require_superadmin` / `require_permission` gate (both resolve identity via `Depends(get_current_user)`; a separate dependency would make PATs invisible to those gates). Extract the PAT logic into a new module `backend/app/auth/pat.py::authenticate_pat(...)` so the security-critical JWT body stays **textually identical**. Add `request: Request` to `get_current_user`'s signature (FastAPI injects it freely).

```
get_current_user(request, credentials, db, session_factory):
    if credentials.credentials.startswith("pat_"):
        return await authenticate_pat(request, credentials.credentials, db, session_factory)
    # ... existing JWT path, unchanged ...
```

**`authenticate_pat` flow:**
1. Compute `HMAC-SHA256(secret)` under `API_TOKEN_HMAC_KEY`; if no match, retry under `API_TOKEN_HMAC_KEY_PREV` (verify-only). No match → generic 401.
2. Lookup `api_tokens` by `token_hash` (unique index).
3. Reject (generic 401) if `revoked_at is not null` OR `expires_at <= now` (naive-UTC normalized).
4. Load `created_by_user_id`'s `User`. If null (owner deleted) → 401.
5. Require `user.is_active` **AND** `user.is_superadmin` on the **freshly-read row** (SEC-R6d) → else 401. (This is what makes demotion/deactivation an instant kill switch even under the GitHub model.)
6. **Do NOT** apply `token_cutoff` — PATs are deliberately independent of password change / global session invalidation (GitHub model). Documented consequence in UI copy (§9).
7. Enforce scope vs `request.method` (§5), fail-closed → 403 on mismatch.
8. Set `request.state.auth_method = "pat"` and `request.state.api_token_id = <id>` (for the interactive-only guard §7 and audit attribution §8). The JWT branch sets `request.state.auth_method = "jwt"`.
9. Bind structlog context (user_id, org_id, role) as the JWT path does; throttled `last_used_at` / `last_used_ip` stamp (§8).
10. Return the `User`.

**Rejection responses (SEC-R8):** one generic `401 "Invalid or expired token"` for unknown/revoked/expired/inactive/not-superadmin alike — no oracle distinguishing the states in the HTTP body. The true reason goes to the audit/log path (§8), not the response.

**Optional-auth endpoints (SEC re-review Finding 6 — accepted for v1):** `get_current_user_optional` (deps.py:79) gets **no** `pat_` branch in v1, so a PAT sent to an optional-auth route (e.g. `/auth/status`) resolves to anonymous rather than authenticated. This is acceptable — PATs target the main authenticated API and fail *toward less access* — but the UI usage help should note that optional-auth/public endpoints don't recognize PATs, to pre-empt "my token doesn't work on X" confusion.

---

## 7. Interactive-session-only surface (the carve-outs)

A single dependency **`require_interactive_session`** (in `app/auth/pat.py` or `app/auth/permissions.py`) raises `403` when the request was not authenticated by an interactive session. **Ordering (SEC re-review Finding 2):** it must declare `Depends(get_current_user)` in its own signature so the `request.state.auth_method` stamp is guaranteed to run first (FastAPI does not order sibling dependencies; `get_current_user` is request-cached, so this is free). It reads state defensively — `getattr(request.state, "auth_method", None) != "jwt"` → deny — so an unset value stays fail-closed. It is added to every route in the following categories, and an **enumeration test** asserts PAT → 403 for each.

**A. Token management** (prevents token-mints-successor-token — SEC-R2 / ARC-R5):
- `POST/GET/DELETE /api/v1/system/api-tokens*`

**B. Account-takeover surface** (a PAT must never be able to permanently own the human account — SEC-R2):
- Password change, email change, MFA enable/disable, recovery-code regeneration, and creating / promoting / role-granting / deleting users.

**C. Tier-0 destructive ops** (operator chose the strict posture — a PAT can never trigger irreversible platform damage — SEC-R3):
- Org-data wipe, org reset, role/permission edits, KEK rotation, feature-flag toggles (`/system/features`), **broadcast send**, override sweep.
- These map closely to the existing "sensitive admin/org actions are audited" set — reuse that inventory as the checklist.

**Residual risk documented:** categories B and C are a **deny-list**; a newly-added destructive endpoint must remember to add `require_interactive_session`. The enumeration test + a code-review checklist item mitigate this. A positive PAT-callable allowlist is the stronger model, deferred to the fine-grained-scope phase (§12).

**Consequence to surface (both reviews):** the global "sign out everywhere" / session-invalidation button does **not** kill PATs. The security-settings UI must say so, and offer "revoke all tokens" alongside it (§9).

---

## 8. Endpoints — `APIRouter(prefix="/api/v1/system/api-tokens")`, superadmin-gated

All three gated by the superadmin permission dependency **and** `require_interactive_session` (§7A).

### `POST /` — mint
- **Step-up, reconciled (SEC-R4 + ARC-R1) — no new ceremony, mirror `users.py`:**
  - `password_set` superadmin → require `current_password` in body, verified inline via `verify_password`.
  - SSO superadmin (`password_set=False`) → require a fresh SSO `stepup_token` (constant-time compare + expiry), per the `users.py` idiom, and **consume it on success** (null `stepup_token`/`stepup_token_expires_at`, matching `users.py:173-174`) so it can't be replayed across mint + another sensitive action within its window (SEC re-review Finding 4).
  - **Additionally**, if `user.mfa_enabled` (the canonical flag, `user.py:99` — NOT `totp_secret` non-null, which is set mid-enrollment before confirmation, SEC re-review Finding 3) → require a fresh TOTP `code`, verified via `verify_totp(decrypt_secret(user.totp_secret), code)` (the `auth.py` pattern). **Do not** require MFA for operators without it. Live re-verification = zero replay window.
- **Body:** `name`, `scope` (`read`|`write`), `expires_in_days` (validated `1 ≤ n ≤ API_TOKEN_MAX_EXPIRY_DAYS`, default `API_TOKEN_DEFAULT_EXPIRY_DAYS`); cap enforced **server-side**, never trust the client (SEC-R7). *Footnote (impl): the cap is enforced at the Pydantic schema layer, so a cap-exceeded request returns `422` before the handler runs and is therefore NOT audited — only the security-relevant step-up failure produces an `api_token.created` failure row. A benign over-cap 422 is intentionally unaudited.*
- **Response:** the plaintext token **once** + metadata. `Cache-Control: no-store` (SEC-R5). The plaintext appears **only** in this response body — never in logs, structlog fields, or the audit row.
- **Rate limit (ARC-R2):** `@limiter.limit(...)` (e.g. `"10/hour"`), registered in the catalogue as `api_tokens.mint`. This is the only expensive/sensitive path; no per-token counter in v1.
- **Audit:** `api_token.created` on success **and** failure (bad step-up, cap exceeded). Detail = name/scope/expiry/prefix + `created_by`. **Never the secret** (SEC-R5). On mint, also **email + in-app notify** the superadmin (SEC-R6a) via `send_notification_email` + the notification system, so an attacker-minted token is visible.

### `GET /` — list
- Metadata only (name, prefix, scope, created, expires, `last_used_at`, status = active/expired/revoked). Never secrets. Modest `@limiter.limit`.

### `DELETE /{id}` — revoke (soft)
- Sets `revoked_at` (row retained for history/audit). Instant effect (auth hits the DB every request). Audit `api_token.revoked`.

### `POST /revoke-all` — panic button (SEC-R6c)
- Revokes all of the caller's active tokens in one call; surfaced in the UI next to "sign out everywhere". Audit `api_token.revoked_all` with a count.

**Audit attribution (SEC-R10 / ARC-R12):** any audit event produced by a **PAT-authed** request records `api_token_id` (threaded via `request.state`) so a leaked token's actions are forensically separable from the human's.

---

## 9. Frontend — `/system/api-tokens` (superadmin-gated)

- **List table:** name, prefix (`pat_a1b2c3…`), scope, created, expires (with an amber/red "in N days" indicator + expired/revoked badges), last used, status; per-row **Revoke** (confirm modal).
- **Mint flow:** form (name, **scope radio** — "Read-only" vs "Read & write", expiry preset select `7 / 30 / 60 / 90` days, default **30**, capped 90) → **step-up modal** (password and/or TOTP as applicable to the operator) → **reveal-once panel**: full token shown once, copy button, "you won't see this again" warning.
- **Panic button:** "Revoke all tokens", placed with the account-security / "sign out everywhere" controls.
- **Security copy (both reviews):** state plainly that PATs **survive password change and global session invalidation** — the only kill switches are revoke and expiry — and that a `read` token can read all-org data. This prevents an operator reaching for the wrong lever during an incident.
- **Inline usage help (v1 docs):** curl examples, scope semantics, the "some POST endpoints are reads and need `write`" caveat, expiry/rotation guidance. No public product docs in v1.
- DESIGN.md token compliance (no off-token colors), superadmin-gated nav entry, empty state.

---

## 10. Expiry reminders — platform-level scheduled job (ARC-R10)

- **Not** a per-org `scheduler.`-namespaced `OrgSetting` job (that namespace is per-org and the runner iterates orgs). PAT reminders are a **platform** concern → implement as a **system-level job** with its own `is_due`, gated by a **`SystemSetting`** flag (e.g. `api_token_expiry_reminders_enabled`), outside the per-org registry.
- **Behavior:** daily scan of non-revoked, non-expired tokens with a non-null owner; per threshold (**14 days, 3 days, on expiry**) send **email + in-app notification** to the `created_by` user, then advance `reminder_stage`.
- **Idempotency (ARC-R10):** the stage-advance and the send must be observed atomically (or the send made idempotent) so a double-run tick cannot double-notify. Skip tokens whose owner is null (SET NULL).

---

## 11. Audit event taxonomy

| Event | When | Notes |
|---|---|---|
| `api_token.created` | mint success/failure | detail: name/scope/expiry/prefix/created_by; **never secret** |
| `api_token.revoked` | single revoke | |
| `api_token.revoked_all` | panic button | detail: count |
| `api_token.auth_rejected` | a **known** token used after revoke/expiry | **audit row only for known-but-dead tokens**; unknown `pat_<garbage>` logs via **structlog only** to avoid an audit-flood DoS (ARC-R12) |

All PAT-authed actions carry `api_token_id` in audit detail (§8).

---

## 12. Backlog spun off from this design

1. **Fine-grained per-permission scopes** — map operations → permission catalogue → phased rollout, most-frequent-first; replaces method-scoping with a positive PAT-callable allowlist. Lands with the per-user phase.
2. **Per-user (customer-facing) PATs** — the data model already supports it (owner-inherited org-scoping).
3. **Public product documentation** — for the customer-facing phase.
4. **Per-token rate-limit counter** — standalone fail-open Redis counter (own env knob, explicitly *not* in the override catalogue), if abuse throttling beyond the mint limit is ever needed.
5. **Optional SalesForce-style coupling** — a per-deployment toggle to make password rotation also revoke PATs (v1 default stays GitHub-style independent).
6. **Optional IP allowlist per token** — further shrink a leaked token's usefulness.

---

## 13. Config additions (document in ENVIRONMENT.md)

| Var | Default | Notes |
|---|---|---|
| `API_TOKEN_HMAC_KEY` | unset → dev-only fallback to `derive_hmac_key(b"api_token")` | **Required in prod, enforced by an explicit `model_validator`** that raises when `app_env == "production" and not set` (do NOT just mirror the optional MFA validator — §3 Finding 1). When present: validated `≥ 32` chars and `!= jwt_secret_key`. |
| `API_TOKEN_HMAC_KEY_PREV` | unset | Verify-only fallback for graceful pepper rotation. |
| `API_TOKEN_DEFAULT_EXPIRY_DAYS` | `30` | Mint default. |
| `API_TOKEN_MAX_EXPIRY_DAYS` | `90` | Server-side hard cap. |

---

## 14. Testing focus

- **Auth path:** PAT valid/expired/revoked/unknown/owner-deleted/deactivated/demoted → correct generic-401 vs success; JWT path unchanged (regression).
- **Scope, fail-closed:** `read` PAT → GET ok, POST/PUT/PATCH/DELETE → 403; `write` PAT → all ok; unmapped method → deny.
- **Interactive-only enumeration test (SEC-R2):** iterate categories A/B/C routes, assert PAT → 403.
- **Step-up matrix:** password-set vs SSO vs MFA-enabled operators; missing/wrong proof → reject; correct → mint.
- **Expiry cap:** `expires_in_days > max` rejected server-side.
- **Reveal-once / no-leak:** assert the secret string appears in **no** audit payload and **no** log field; `Cache-Control: no-store` present.
- **Reminder job:** thresholds fire once each (idempotent stage-advance); null-owner skipped; platform-level (not per-org).
- **Pepper rotation:** token minted under `_PREV` still verifies; minted under primary verifies; rotation doesn't brick.
- **Migration:** up/down on isolated real-MySQL `-p team-*` stack.
- **`last_used_at` throttle:** not written on every request.

---

## 15. Ruling traceability (for the post-spec security re-review)

**Security review — APPROVE-WITH-RULINGS:** R1 §3 (dedicated pepper) · R2 §7 (interactive-only surface, positive enforcement) · R3 §5 (central fail-closed scope, honest framing) + §7C (Tier-0 carve-out) · R4 §8 (fresh action-bound step-up) · R5 §8/§9 (reveal-once transport) · R6 §2/§9 (compensating controls for password-independence) · R7 §8/§13 (30d default cap) · R8 §3/§6 (generic 401, hash choice) · R9 §8 (throttled `last_used_at`) · R10 §8/§11 (per-token audit attribution; limiter is abuse-cap).

**Architecture review — APPROVE-WITH-RULINGS:** R1 §8 (step-up mirrors `users.py`) · R2 §8 (rate-limit mint only, not per-token catalogue) · R3 §6 (`authenticate_pat` helper, branch in `get_current_user`) · R4 §5 (scope in auth path, not middleware) · R5 §7 (`request.state` + `require_interactive_session`) · R6 §4 (String scope, no MySQL ENUM) · R7 §4 (naive-UTC + `_aware`) · R8 §4 (model/migration registration + real-MySQL verify) · R9 §4 (`ON DELETE SET NULL` + snapshot) · R10 §10 (platform-level reminder job) · R11 §4/§8 (`last_used` throttle + `get_client_ip`) · R12 §11 (`auth_rejected` flood guard).

**Spec-level security re-review — APPROVE-WITH-CHANGES (all folded):** F1 §3/§13 (explicit prod-required validator, not the optional MFA one — the load-bearing fix) · F2 §7 (`require_interactive_session` takes `Depends(get_current_user)` for ordering, defensive `getattr`) · F3 §8 (MFA trigger = `user.mfa_enabled`, not `totp_secret` non-null) · F4 §8 (consume SSO `stepup_token` on mint) · F5 §3 (`derive_hmac_key(b"...")` bytes + `.encode()` primary path) · F6 §6 (optional-auth endpoints don't recognize PATs in v1 — accepted, fails toward less access). One implementer wiring note carried forward: the platform reminder job must be invoked from `run_one_tick` (loop.py:25) under the existing `scheduler:tick:lock`, since it can't join the per-org `REGISTRY`.
