# MFA recovery-code HMAC key decouple

**Status:** approved, ready for implementation
**Effort:** small (~15-20 LOC + docs)
**Decision basis:** 3-architect security panel, 2026-07-14 (unanimous on model, 2-1 on the `_prev` question)

## Problem

MFA recovery codes are stored as keyed HMAC-SHA256 hashes. Today **all** MFA key
material derives from `jwt_secret_key`:

- `hash_recovery_code` → `derive_hmac_key(RECOVERY_CODE_PURPOSE)` = `HMAC(jwt_secret_key, purpose)`
- `_hash_recovery_code_legacy` → raw `jwt_secret_key` (pre-derivation scheme, permanent verify-only fallback)

`derive_hmac_key` is computed at call time, so **rotating `jwt_secret_key`
instantly invalidates every stored recovery-code hash** under both schemes. The
existing permanent legacy fallback protects against the *derivation migration*,
not against a jwt rotation — after a rotation neither jwt-based candidate matches.

We want to be able to rotate `jwt_secret_key` freely (incident response,
"invalidate all sessions", suspected leak) without bricking recovery codes.

## What this is — and is NOT

This is an **operational rotation-decoupling** feature. It is **not** a
key-separation upgrade: `derive_hmac_key` already gives recovery codes a distinct
key from JWT signing and from email codes. A dedicated env var does not
meaningfully improve cryptographic separation over the existing purpose
derivation — its only value is an **independent rotation lifecycle** for
newly-minted hashes.

### The decoupling is asymptotic, not immediate (state this in the PR)

On the day the dedicated key is set, **0%** of the existing hash population is
protected — every stored hash was minted under a jwt-based scheme and still
bricks on a jwt rotation. Protection grows only as users **regenerate** their
codes (regeneration overwrites a user's entire `recovery_codes` list, so each
user's list is homogeneous — fully legacy or fully dedicated, never mixed;
verified against both regenerate sites, `auth.py:2353` and `2497`, which
overwrite the entire `recovery_codes` string). The
permanent jwt fallback caps the protected fraction below 100% forever unless the
operator runs a one-time **forced-regeneration campaign** after adoption. For
this app's small userbase that campaign is a clean, optional way to fully sever
the jwt dependency.

Accurate phrasing: *"a `jwt_secret_key` rotation no longer invalidates recovery
codes for users who enrolled or regenerated after the dedicated key was adopted."*

## Design

### Adoption model — A (optional dedicated key + permanent jwt-derived fallback)

- If `MFA_RECOVERY_HMAC_KEY` is **set**, it is the primary key for new/regenerated
  hashes; verify chain becomes **dedicated → jwt-derived → raw-jwt-legacy**.
- If **unset**, behavior is byte-for-byte identical to today (2-layer chain).
- Operator adopts the decouple by simply setting the env var. No migration, no
  downtime, no forced regen required to ship.

Rejected alternatives:

- **B (required / refuse-boot):** buys zero security over A — the jwt-derived and
  raw-jwt layers stay in the verify chain regardless, so old hashes still depend
  on jwt either way — while turning an upgrade into a fleet-wide boot outage
  (dev, CI, existing prod). Pure friction.
- **C (no jwt fallback when set):** bricks every un-regenerated hash on flip-on.
  Violates the hard requirement.

### Config (`backend/app/config.py`)

- Add `mfa_recovery_hmac_key: str = ""`. Empty default ⇒ backward-compatible
  (fall through to jwt-derived path).
- Add a validator that **no-ops when empty** (empty is the backward-compatible
  no-op path — there is no separate placeholder sentinel to reject, unlike
  `jwt_secret_key`), and when non-empty enforces:
  - `.strip()` first; a stripped-empty value is treated as the empty no-op (else
    32 whitespace chars would pass the length check)
  - `len >= 32` (parity with the `jwt_secret_key` rule) — also catches short junk
  - **reject equality with `jwt_secret_key`** — a copy-paste of the jwt value
    silently re-couples the two and defeats the entire feature. This needs
    cross-field access, so implement as a `model_validator` (mode="after") rather
    than a bare `field_validator`.
- Do **not** default it to the jwt value. Do **not** add it to the lifespan
  KEK-collision guard (that guard exists for *reversible Fernet* KEKs; an HMAC
  key is a different case and the validator above already covers the collision).

### Hashing (`backend/app/services/mfa_service.py`)

- `hash_recovery_code(code)`: mint under the dedicated key **when set**, else fall
  through to today's `derive_hmac_key(RECOVERY_CODE_PURPOSE)`.
  - The dedicated key is used **directly** as the HMAC key. It is already a
    single-purpose secret; the derivation layer exists only to avoid reusing the
    *multi-purpose* `jwt_secret_key` raw, and that rationale does not apply to a
    purpose-dedicated key. (Keeps the change minimal; 2 of 3 architects endorsed
    using it directly.) The key is a `str`, so `.encode()` it to bytes before
    `hmac.new`, mirroring `_legacy_hmac_key`'s `settings.jwt_secret_key.encode()`.
- `verify_recovery_code(code, hashed_codes)`: build the candidate list up front —
  dedicated candidate **only when the key is set** (do not fabricate a candidate
  when unset), plus jwt-derived, plus raw-jwt-legacy. Compare every stored entry
  against every candidate with **no early break** (preserve the existing
  constant-time discipline; the chain length varies only by static config, never
  by user input or secret). At most one candidate can match any stored entry, so
  verify order is irrelevant to correctness. "No early break" means: no `break`
  out of the loop, and prefer a uniform compare of every entry against every
  candidate over the current `if/elif` (which skips the second compare on a
  match). The `if/elif` asymmetry only diverges on a legitimate success and is
  not attacker-observable, so it is acceptable — but honoring the uniform form
  keeps a reviewer from flagging the `elif` either way.
- Preserve single-use semantics unchanged: the sole caller pops the matched index
  after a successful verify (and commits only after `_issue_tokens` succeeds).
  **No lazy re-hash on match** — a re-hashed entry would be immediately discarded
  by the pop, and the prohibition guards a latent bug if someone later stops
  popping.

### Explicitly NOT built

- Required-boot refusal (model B).
- A `_PREV` fallback slot for the dedicated key (see rotation section for why).
- A lifespan KEK-collision-guard entry.
- Any lazy in-place hash migration.
- No repointing of `mfa_email_code_hmac` — email codes have no stored hash and no
  fallback need (they live only inside a 10-minute signed token); out of scope.

## Rotation runbook

### jwt_secret_key rotation after adoption

- Hashes minted under the dedicated key: **no-op** (independent lifecycle). This
  is the win.
- Any still-stored jwt-derived / raw-jwt-legacy hashes: **still brick** on jwt
  rotation — those users lose the recovery path until they re-enroll/regenerate
  (they can still use TOTP / email MFA). Before rotating jwt, run an ops query to
  count users whose `recovery_codes` predate adoption so the blast radius is known.
- Do **not** remove either jwt fallback layer as part of a jwt rotation — that
  would brick the legacy population immediately rather than lazily.

### Rotating MFA_RECOVERY_HMAC_KEY itself (compromise)

Recovery hashes are **one-way** — the stored plaintext is unknown, so a stored
hash can never be re-HMACed / migrated in place. The only remediation is to swap
the key and **force every user to regenerate** their recovery codes (invalidate
the stored set).

### Unsetting the key after adoption (rollback direction)

Symmetric to a jwt rotation: once users have regenerated under the dedicated key,
**unsetting** `MFA_RECOVERY_HMAC_KEY` drops the dedicated candidate from the
verify chain, so those dedicated-scheme hashes stop verifying and those users
lose their recovery path until they regenerate. This is inherent one-way-hash
behavior (removing a key invalidates hashes under it), not a bug. "Unset = no-op"
holds only for a system that was *never* set; a set→unset rollback is not free.

**Why no `_PREV` slot** (the 2-1 panel decision — document so nobody "fixes" it):
a `_prev` fallback would let *old-key* hashes keep verifying after a swap, which
is exactly the wrong behavior when the old key is the compromised one — you want
a hard cutover. For non-compromise *hygiene* rotation a `_prev` also buys little,
because one-way hashes force a regeneration campaign regardless. This is the key
contrast with the **reversible** Fernet KEK `_prev` pattern (`ai_credential_*`,
`mfa_encryption_key`): Fernet can decrypt-old / re-encrypt-new for a genuine lazy
migration; one-way HMAC has no equivalent, so do not copy that pattern here.

## Ship-list (prod adoption)

Setting the key in production requires (per the DO spec-sync trap — `.do/app.yaml`
is authoritative and `EV[]` blobs must land in the repo):

- `.env.example` — document the var + generation hint
  (`python -c 'import secrets; print(secrets.token_urlsafe(64))'`)
- `ENVIRONMENT.md` — add to the source-of-truth env table
- `.do/app.yaml` — add the `EV[]` secret entry
- KEK rotation playbook — a short note describing the jwt-rotation-is-now-safe
  behavior and the compromise/forced-regen path

Shipping the *code* does not require setting the key anywhere — unset is fully
backward compatible. The ship-list is only for when the operator chooses to adopt.

## Test matrix

- Unset key ⇒ verify behavior byte-identical to today (2-layer chain); a hash
  minted under the jwt-derived scheme still verifies; a raw-jwt-legacy hash still
  verifies.
- Set key ⇒ `hash_recovery_code` mints under the dedicated key; that hash verifies.
- Set key ⇒ a hash minted under the jwt-derived scheme **still** verifies (the
  must-not-brick requirement), and a raw-jwt-legacy hash still verifies.
- A stored set mixing all three schemes verifies each correctly, single-use pop
  removes exactly the matched entry.
- Validator: rejects `len < 32` (incl. stripped-whitespace junk), rejects a value
  equal to `jwt_secret_key`; accepts empty / stripped-empty (no-op) and a valid
  distinct key. (There is no placeholder sentinel for this var — empty is the
  no-op path — so there is nothing to reject as a placeholder.)
