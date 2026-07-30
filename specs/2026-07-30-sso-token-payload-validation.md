# Validate both Google 200 bodies at both callback sites (TBD-267)

Status: design settled 2026-07-30. Two independent architects, converged. Branch
`fix/TBD-267-sso-token-payload`. Backend only. No migration, no schema change, **no frontend
change** — both UI codes this uses are already mapped in all three copy dicts.

---

## 0. Correction — the ticket's "already `.get()`-guarded, do not touch" claim is FALSE

TBD-267 asserts that all `google_user` field reads are already `.get()`-guarded and need no work.
`.get()` guards a **missing key**. It does not guard a **non-dict payload**. A userinfo 200 whose
body decodes to a list, a string or a number makes `google_user.get(...)` raise `AttributeError`,
and — critically — that read sits **outside the `try` block entirely**. See §2.

The ticket's defect is real. Its prescription for half of it is not.

## 1. The defect — six reachable lines, one root cause

Both Google OAuth callback sites in `backend/app/routers/auth.py` trust a 200 response body
without validating its shape. Against `main` at `cd39afc5`:

| Site | Line | Expression | Raises on |
|---|---|---|---|
| `google_callback` (login) | 2973 | `tokens = token_resp.json()` | `JSONDecodeError` — 200 carrying HTML |
| | 2980 | `tokens['access_token']` | `KeyError` (no key) / `TypeError` (body is a list) |
| | 2987 | `google_user = userinfo_resp.json()` | `JSONDecodeError` |
| | **3045** | `google_user.get("email", "")` | **`AttributeError` — outside the `try`** |
| `sso_stepup_callback` | 3506 | `tokens = token_resp.json()` | `JSONDecodeError` |
| | 3512 | `tokens['access_token']` | `KeyError` / `TypeError` |
| | 3516 | `google_user = userinfo_resp.json()` | `JSONDecodeError` |
| | **3537** | `google_user.get("email")` | **`AttributeError` — outside the `try`** |

Only `except TimeoutError:` (2988 / 3517) and `except httpx.HTTPError:` (3022 / 3534) exist. There
is no app-level `Exception` handler. Every one of these therefore produced a **bare 500**: no audit
row, and DigitalOcean App Platform's generic error splash instead of the friendly `?sso_error=`
banner — the exact user-visible failure the friendly-redirect work existed to remove.

The most likely production shape is mundane: Google answers the token exchange **200** with
`{"error": "invalid_grant"}` for a replayed or expired authorization code.

## 2. Why validation, and not catching — the argument that settles it

Lines 3045 and 3537 are **after** the `try/except` block, on the main line. An `AttributeError`
raised there is reachable by **no `except` clause at any width**: not by widening
`except TimeoutError`, not by adding a third clause, not by `except Exception`. The only way to
make it not happen is to never let a non-dict reach `google_user`.

That is not a preference. It is a proof that shape validation is the only fix that closes the whole
defect, and it is executable: fences **L8** and **S7** stub a list-bodied userinfo 200 and cannot
be passed by any exception-handling change whatsoever. §6 records the injection run that
demonstrates it.

Consequence for the mechanism: **zero new `except` clauses.** The set of exceptions each handler's
`try` catches is byte-identical before and after this change, which is what keeps TBD-179's
upper-bound fences (**L5**, **S5**) valid without re-deriving them. Both were verified to pass
byte-unmodified by the final implementation.

## 3. The mechanism — two pure helpers, four inline guards

Two module-level helpers next to `_google_error_redirect`:

- **`_google_json_object(resp) -> dict | None`** — decodes, returns the payload only when it is a
  `dict`, `None` otherwise. `except ValueError` **and nothing wider**: httpx's `.json()` is
  `json.loads(self.content)`, whose only body-dependent failures are `json.JSONDecodeError` and
  `UnicodeDecodeError`, both `ValueError`. Anything else out of `.json()` is our bug and keeps
  propagating. It **returns** rather than raises, deliberately — a value the caller branches on adds
  no exception surface at all.
- **`_google_token_body_detail(tokens) -> dict`** — the forensic detail for a token body we could
  not use, and the **only** place in the change that reads a field off an untrusted Google token
  body. Emits a shape word plus, when present, the OAuth2 `error` code truncated to 64 chars.
  Never the body, never any other field: a partially-valid token payload carries `access_token` /
  `refresh_token` / `id_token`, and this dict is persisted to `audit_events.detail` and rendered in
  `/admin/audit`.

Four inline guards that `audit; return` from inside the existing `try`, exactly like the adjacent
non-200 branches already do. The userinfo non-200 branches (2983-2986, 3514-3515) are untouched:
they key on `status_code` only and are mutually exclusive with the new guards, which sit strictly
after them.

### 3.1 The hoist is mandatory, not cosmetic

`tokens['access_token']` was evaluated **inside** the second `asyncio.timeout_at` scope — it is an
argument expression to the bounded `client.get`. It was the only non-transport-raising expression
in any bounded block at either site, and therefore a standing leak in the TBD-179 spec §2.1
invariant that bounded blocks contain only the network await. The fix hoists it to a local
`access_token` computed before the block. After the hoist each bounded block can raise only a
transport error or `TimeoutError`.

`progress["phase"] = "token_ok"` stays immediately before the userinfo bounded block, so
`last_phase` still means "the last phase that completed successfully" — TBD-179's **L2**/**S2**
depend on that reading.

### 3.2 The `isascii()` check is not pedantry

httpx encodes header values with `value.encode("ascii")` (`httpx/_models.py:82`) while **building**
the request, and that build happens inside the bounded block. A non-ASCII access token would
therefore raise `UnicodeEncodeError` past both `except` clauses — a seventh reachable 500. Hence
the guard rejects a non-ASCII token as `unusable_access_token`.

An ASCII-but-illegal value (embedded CRLF) needs no check: h11 raises `httpx.LocalProtocolError`,
an `httpx.HTTPError` subclass, already handled.

## 4. Audit vocabulary

| | login site | step-up site |
|---|---|---|
| `event_type` | `auth.google.callback.failed` | `auth.google.sso_stepup.callback.failed` |
| token guard `reason` | `token_payload` | `token_payload` |
| userinfo guard `reason` | `userinfo_payload` | `userinfo_payload` |
| redirect `ui_code` | `token` / `userinfo` | `token` / `userinfo` |
| `actor_email` | `""` (no email known yet) | `user.email` |

The sub-case rides in `detail["body"]` ∈ `not_object | no_access_token | unusable_access_token`,
plus `detail["google_error"]` when the token body carried one. This mirrors TBD-179's
`reason="timeout"` + `detail_extra={"last_phase": ...}` precedent exactly: a distinct audit reason
so a new failure class shows up as a previously-empty bucket filling, and a reused UI code so the
user gets copy that already exists.

Deliberate asymmetries, each load-bearing:

- **No `google_error` on the userinfo branch.** The userinfo body has no RFC error contract to read
  one from, and it does carry PII.
- **No `error_description`.** Unlike the provider-error branch, which carries it because there is no
  token body in play there, here it is free text arriving alongside credential material.
- **The two sites are not merged into a shared failure helper.** The step-up guards return through
  the existing `_stepup_failure`, which is what inherits its own `event_type`, its
  `_resolve_return_path(state)` target, its `/api/v1/auth/sso-stepup` cookie path and its
  `actor_email`. `ui_code=` is passed **explicitly**: `_stepup_failure` otherwise derives the
  redirect code from the audit reason and would emit the unmapped `token_payload`, silently
  degrading to the fallback banner.

## 5. Observability — the ungated warning is required

Both guards emit an ungated `_LOGGER.warning("auth.google.callback.invalid_payload", ...)` with
flat kwargs (never `extra=` — `_LOGGER` is a structlog stdlib BoundLogger, which renders `extra` as
a nested object and breaks a DigitalOcean filter on `flow:"login"`).

This is a **net-visibility** requirement, not decoration. Today this failure class is loud in the
worst possible way: a 5xx stack trace in the platform logs. After the fix it is a quiet 307 that
looks exactly like an ordinary user-side failure. Shipping the guard without the warning would
trade a bare 500 for a *silent* one.

## 6. Fence and guard table, with injection evidence

### 6.1 Step 0 — the harness was itself vacuous, and was fixed first

`test_auth_google_callback_errors.py` resolved stub payloads by **truthiness**:
`self._payload = payload or {}` and `token_payload or {"access_token": "fake-token"}`. So
`token_payload={}` and `token_payload=[]` — two of the exact bodies these fences must drive —
silently became the *success* payload, and a fence stubbing them would have passed **vacuously
against unmodified `main`**. Both were changed to explicit `is None` resolution and the payload
type hints widened to `Any`, with the same treatment applied to `_FakeAsyncClient` in
`test_auth_stepup.py`. New knobs: `token_json_exc` / `userinfo_json_exc` (make `.json()` raise) and
`raise_exc_on` (inject a programmer error at the userinfo GET instead of the token POST). Defaults
keep every pre-existing test byte-identical — verified: 46 passed with `auth.py` untouched.

### 6.2 Gate G0 — every defect fence RED against unmodified `auth.py`, for the right reason

| Fence | Stub | G0 failure mode |
|---|---|---|
| U1 | `.json()` raises `_ProgrammerBug` | `AttributeError: module has no attribute '_google_json_object'` |
| U2 (×7) | decode error / `[]` / `["a","b"]` / `"str"` / `7` / `{}` / `{"a":1}` | same |
| U3 | full token body + 5000-char `error` | `AttributeError: module has no attribute '_google_token_body_detail'` |
| L6 | token 200 `{"error":"invalid_grant",...}` | `auth.py:2980 KeyError: 'access_token'` |
| L7 `json-array` | `token_payload=[]` | `auth.py:2980 TypeError: list indices must be integers or slices, not str` |
| L7 `not-json` | `token_json_exc=JSONDecodeError` | `json.decoder.JSONDecodeError` out of `token_resp.json()` |
| L8 | `userinfo_payload=["not","a","dict"]` | **`auth.py:3045 AttributeError: 'list' object has no attribute 'get'`** |
| L10 | L6's stub, `_LOGGER` patched | `auth.py:2980 KeyError: 'access_token'` |
| S6 | token 200 without `access_token` | `auth.py:3512 KeyError: 'access_token'` |
| S7 | `userinfo_payload=["x"]` | **`auth.py:3537 AttributeError: 'list' object has no attribute 'get'`** |
| S8 | S6 + `return_to: "security"` | `auth.py:3512 KeyError: 'access_token'` |
| S10 | S6's stub, `_LOGGER` patched | `auth.py:3512 KeyError: 'access_token'` |

18 failed / 49 passed at G0. The two bolded rows are lines 3045 and 3537 — §2's argument, executed.

**L9**, **S9** and **L11** are green at G0 by construction: they are negative controls (a programmer
error must propagate; the warning must not fire on the success / non-200 / httpx / timeout paths).
Their gate is the injection in §6.3, not G0.

### 6.3 Step 4 — the WRONG-BROAD injection

**WRONG-BROAD:** the guards removed, and a new `except Exception:` clause added to each handler's
`try`, auditing the **right** `reason="token_payload"` and returning the **right** `ui_code`. The
right strings are deliberate: a version using a wrong reason string would be killed by a mere
reason assertion, and assuming the lazy variant is how a test plan fools itself.

12 red (11 named + L7's second parametrization):

| Test | Failure under WRONG-BROAD |
|---|---|
| L5 | `DID NOT RAISE _ProgrammerBug` — the broad clause swallows a genuine bug |
| S5 | `DID NOT RAISE _ProgrammerBug` |
| L9 | `DID NOT RAISE _ProgrammerBug` (userinfo phase) |
| S9 | `DID NOT RAISE _ProgrammerBug` (userinfo phase) |
| L6 | `assert {'reason': 'token_payload'} == {'body': 'no_access_token', 'google_error': 'invalid_grant', 'reason': 'token_payload'}` |
| L7 (×2) | `assert {'reason': 'token_payload'} == {'body': 'not_object', 'reason': 'token_payload'}` |
| S6 | `assert {'reason': 'token_payload'} == {'body': 'no_access_token', ...}` |
| **L8** | **`auth.py:3112 AttributeError: 'list' object has no attribute 'get'`** |
| **S7** | **`auth.py:3608 AttributeError: 'list' object has no attribute 'get'`** |
| L10 | `AssertionError: []` — no warning emitted at all |
| S10 | `AssertionError: []` |

L8 and S7 still raise `AttributeError` **under a live `except Exception:`**. That is the whole
argument, reduced to a stack trace: the broadest clause Python has does not reach these lines,
because they are not inside the block.

**S8** and **L11** stay green under WRONG-BROAD, as designed. S8 kills a *hand-rolled redirect*
(WRONG-BROAD routes through `_stepup_failure` and gets the target right); L11 is the emitter's
negative control (WRONG-BROAD emits nothing, so all four silent legs hold).

Restored → **67 passed**.

### 6.4 What each guard kills

| Fence | Kills |
|---|---|
| U1 | `except Exception` inside `_google_json_object` — the one widened-clause hazard the change introduces, nested one frame deeper than L5/S5 can see |
| U2 | (a) an `"access_token" in tokens` fix that leaves the `AttributeError` alive; (b) a helper returning `{}` instead of `None`, collapsing `not_object` into `no_access_token` |
| U3 | a "dump the body to debug this" edit leaking a bearer token into `audit_events.detail` and `/admin/audit` |
| L6/L7/S6 | the token-side defect, with the audit `detail` pinned as an **exact dict** |
| L8/S7 | the userinfo-side defect — unreachable by any `except` |
| L9/S9 | an `except Exception` wrapped around the userinfo half only (L5/S5 drive from the token POST and would stay green) |
| L10/S10 | deleting the ungated warning; also the only end-to-end exercise of the `unusable_access_token` branch, driven with a sentinel credential asserted absent from the log line |
| L11 | the emitter hoisted onto the main line, into a `finally`, or into the `except` clauses; also re-asserts the timeout branch still audits `reason="timeout"` |
| S8 | a guard that hand-rolls its own redirect with the default `/settings` target instead of routing through `_stepup_failure` → `_resolve_return_path` |

### 6.5 One ruling correction found during execution

The design prescribed `assert "access_token" not in str(fields)` as L10/S10's credential check.
That assertion is **unsatisfiable against the design's own vocabulary**: the shape word is
`no_access_token`, which contains the substring. Replaced with two stronger checks — an exact
key-set assertion on the emitted fields in both legs, and a second leg driving a **real** credential
(a non-ASCII `access_token` carrying a `SENTINEL` marker) through the `unusable_access_token`
branch, asserting the marker never reaches the log line. The stub the original assertion used
contained no credential at all, so it could not have fenced a leak either way.

## 7. Scope

**IN:** `backend/app/routers/auth.py` (two helpers, four guards, two hoists);
`backend/tests/routers/test_auth_google_callback_errors.py`;
`backend/tests/routers/test_auth_stepup.py`; these two spec files.

**OUT, each on the merits:**
- **Frontend.** `token` and `userinfo` are already mapped with correct copy in all three copy dicts
  (`frontend/components/auth/LoginPageBody.tsx:32-45`, `frontend/app/settings/page.tsx:20-32`,
  `frontend/app/settings/security/page.tsx:46-58`). A new code would mean *worse* copy, not better.
- **The userinfo non-200 branches.** They key on `status_code` only and are mutually exclusive with
  the new guards. Not touched, not re-indented.
- **Merging the two sites into a shared failure helper.** §4.
- **A third `except` clause anywhere.** §2.
