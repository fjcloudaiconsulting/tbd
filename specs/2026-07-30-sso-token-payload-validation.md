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

---

## 8. Post-merge review fold (TBD-267 follow-up)

Two independent reviews of the merged PR #598 found four correctness defects and six mutations
that stayed green. Every finding was reproduced by execution before it was fixed, and every fix
was confirmed RED-then-green. **C1-C3 are pre-existing gaps #598 did not close rather than
regressions it introduced** — but #598 claimed to close them, so the claim was false in the repo
until this fold.

### 8.1 C1 — `except ValueError` was too narrow, and its justification was FALSE

The shipped justification — "httpx 0.28's `.json()` is `json.loads(self.content)`, whose only
body-dependent failures are `JSONDecodeError` and `UnicodeDecodeError`, both `ValueError`" —
appeared verbatim in three places: the `_google_json_object` docstring, U1's docstring, and the
merged PR body. It is wrong. A deeply nested JSON body makes `json.loads` raise **`RecursionError`**
(MRO `RecursionError → RuntimeError → Exception`), which is not a `ValueError`.

Measured in-container, Python 3.12.13 / httpx 0.28.1, `sys.getrecursionlimit() == 1000`:

| nesting depth | body size | result |
|---|---|---|
| 200 – 8000 | ≤16 KB | decodes fine |
| **10000** | 20 KB | **`RecursionError`** |
| 20000 | 40 KB | `RecursionError` |

The limit is a **C-stack** check, not `sys.getrecursionlimit()`, so the threshold is not a fixed
number. The fences therefore use depth 20000 and a factory (`_recursion_error()`) that **asserts
the raise actually happened**; a depth that quietly stopped tripping would otherwise turn every
fence built on it into a silent pass.

End-to-end at both sites the exception escaped the helper and the handler: **`audit rows == []`**,
bare 500 — exactly the defect #598 exists to remove.

**Fix:** `except (ValueError, RecursionError)`. Both in-repo copies of the justification corrected
(helper docstring + U1 docstring); the third copy is the merged PR body and is immutable.

**Evidence.** New fences RED against the un-fixed code:

```
FAILED test_google_json_object_absorbs_a_real_recursion_error
FAILED test_google_json_object_returns_none_for_every_non_object_body[recursion-error]
FAILED test_token_200_with_a_non_object_body_redirects_and_audits[recursion]
FAILED test_userinfo_200_with_a_non_object_body_redirects_and_audits[recursion]
FAILED test_stepup_token_200_without_access_token_redirects_and_audits[recursion]
FAILED test_stepup_userinfo_200_with_a_non_object_body_redirects_and_audits[recursion]
/usr/local/lib/python3.12/json/decoder.py:354: RecursionError: maximum recursion depth
  exceeded while decoding a JSON array from a unicode string
```

Re-narrowing the clause back to `except ValueError` against the fixed code reproduces the same
six reds (**NARROW**, §8.7). The token-phase legs at both sites are the "307 + a `token_payload`
audit row" fence C1 asked for.

### 8.2 C2 — the shape guard validated the container, not the fields

`_google_json_object` proves the userinfo body is a `dict`. It says nothing about what is in it, and
`.get(key, default)` substitutes its default only for a **missing key** — never for an explicit
`null`, a list or a number. The login site's `google_user.get("email", "")` therefore reached
`.strip()` on the main line, **past the last `except`**:

| userinfo body | login site | step-up site |
|---|---|---|
| `{"email": null}` | `AttributeError: 'NoneType' … 'strip'`, rows `[]` | survives (`or ""`) |
| `{"email": ["a@b.io"]}` | `AttributeError: 'list' …` | `AttributeError: 'list' …` |
| `{"email": 12345}` | `AttributeError: 'int' …` | `AttributeError: 'int' …` |

The step-up site's `(google_user.get("email") or "")` rescues only *falsy* values; a list or a number
is truthy, so it broke on both.

**Fix:** a small local helper, `_google_str_field(payload, key, default="")` — return the value if it
is a non-empty `str`, else the default. Applied to the email read at **both** sites. No new audit
reason: a non-string email lands on the **existing** branch each site already has.

⚠ **One correction to the finding as written.** It asked for both sites to land on the existing
`no_email` branch. The step-up handler **has no `no_email` branch** — its existing branch for an
unreadable Google email is **`email_mismatch`** (`auth.py:3708`), which is where an explicit `null`
already landed before this change. So the step-up fence pins `email_mismatch`, not `no_email`; the
intent (an *existing* branch, no invented reason) is honoured. The step-up `event_type`,
`_resolve_return_path(state)` target, cookie path and `actor_email` are all preserved by routing
through the untouched `_stepup_failure`.

**Evidence.** RED against un-fixed code (`L12` × 5, `S11` × 4 — the step-up `null` leg was already
green, and is reported as green rather than dressed up):

```
/app/app/services/user_service.py:36: AttributeError: 'NoneType' object has no attribute 'strip'
/app/app/services/user_service.py:36: AttributeError: 'list' object has no attribute 'strip'
/app/app/services/user_service.py:36: AttributeError: 'int' object has no attribute 'strip'
/app/app/services/user_service.py:36: AttributeError: 'dict' object has no attribute 'strip'
/app/app/services/user_service.py:36: AttributeError: 'bool' object has no attribute 'strip'
/app/app/routers/auth.py:3697: AttributeError: 'list' object has no attribute 'strip'   (step-up)
/app/app/routers/auth.py:3697: AttributeError: 'int' object has no attribute 'strip'
/app/app/routers/auth.py:3697: AttributeError: 'dict' object has no attribute 'strip'
/app/app/routers/auth.py:3697: AttributeError: 'bool' object has no attribute 'strip'
```

### 8.3 C3 — the other userinfo fields were still trusted

Three more uncaught 500s at the login site, all outside the `try`, all on a body that **passes** the
new shape guard:

- `google_user.get("given_name", "")` — `{"given_name": 99, "family_name": 100}` →
  `TypeError: sequence item 0: expected str instance, int found` inside `_suggest_username`'s
  `" ".join(parts)` (`auth.py:157`).
- `_safe_avatar_url(google_user.get("picture"))` — `{"picture": 12345}` →
  `TypeError: object of type 'int' has no len()`.
- `{"picture": {"url": "x"}}` — a dict has `len() == 1`, so it passes `_safe_avatar_url`
  **unchanged**, is assigned to `user.avatar_url`, and dies at commit with
  `ProgrammingError: type 'dict' is not supported` — **after** mutating ORM state.

**Fix:** the same `_google_str_field` helper on `given_name`, `family_name` and both `picture`
reads. **No audit reason:** a wrong-typed display name or avatar is a missing optional field, not a
failure, so the fence asserts the callback **succeeds** with the field dropped and writes **no**
audit row. Blocking a sign-in over an avatar would be worse than the bug.

The step-up site was checked for the same reads and **has none** — it reads only `email`,
`verified_email` and `email_verified`, and the last two go through `bool(...)`, which is total.

**Evidence.** L13 RED on both branches (they read the fields at different places, so a fix applied
to one and not the other passes half the test):

```
new-user      /app/app/routers/auth.py:157: TypeError: sequence item 0: expected str instance, int found
existing-user aiosqlite/core.py:105: sqlalchemy.exc.ProgrammingError: (sqlite3.ProgrammingError)
              Error binding parameter 3: type 'dict' is not supported
              [SQL: UPDATE users SET first_name=?, last_name=?, avatar_url=? …]
              [parameters: (99, 100, {'url': 'https://example.test/a.png'}, 1)]
```

### 8.4 C4 — `_google_token_body_detail` mislabelled a wrong-typed token

`{'access_token': {'nested': 1}}` and `{'access_token': 12345}` both emitted
`{'body': 'no_access_token'}` — telling an operator Google returned **no** token when it returned
one. Different first move: "no token" points at credentials and consent, "wrong type" points at
whatever is rewriting the body between us and Google.

**Fix:** a third shape word, **`bad_access_token_type`**. `null` stays `no_access_token` (JSON has no
other way to spell an unset field) and so does `""` (right type, merely empty). Pinned in U3 and
end-to-end at both sites (L6 `number-token` / `object-token`, S6 `number-token`).

**U3's first fixture was also wrong and is fixed.** It asserted `body == "no_access_token"` for
`{"access_token": "s", …}` — a perfectly usable ASCII token. That describes a state the helper can
never see in production: it is only ever called *after* the guard rejected the token, and the guard
accepts `"s"`. Changed to `"access_token": ""`, which is the real shape that reaches the helper with
other credentials (`refresh_token`, `id_token`) still live alongside it. The asserted value is
unchanged; it is now honest.

### 8.5 The six previously-green mutations, now RED

Run against the fixed code, one at a time, restored between each. Real output:

| Mutation | What it does | Now fails |
|---|---|---|
| **M7b** | delete the userinfo `_LOGGER.warning` at the **login** site | `test_userinfo_200_with_a_non_object_body_redirects_and_audits[json-array]`, `[not-json-at-all]`, `[recursion]` — 3 failed, 92 passed |
| **M7d** | delete the userinfo `_LOGGER.warning` at the **step-up** site | `test_stepup_userinfo_200_with_a_non_object_body_redirects_and_audits[json-array]`, `[not-json-at-all]`, `[recursion]` — 3 failed, 92 passed |
| **M12** | drop `or not access_token` from the guard predicate, **both** sites | `test_token_200_without_access_token_redirects_and_audits[empty-token]`, `test_stepup_token_200_without_access_token_redirects_and_audits[empty-token]` — 2 failed, 93 passed |
| **M17** | step-up token phase decodes raw (`tokens = token_resp.json()`), no shape validation | `test_stepup_token_200_without_access_token_redirects_and_audits[json-array]`, `[not-json-at-all]`, `[recursion]` — 3 failed, 92 passed |
| **M18** | login userinfo: raw `.json()` + an `isinstance` check | `test_userinfo_200_with_a_non_object_body_redirects_and_audits[not-json-at-all]`, `[recursion]` — 2 failed, 93 passed |
| **M19** | step-up userinfo: raw `.json()` + an `isinstance` check | `test_stepup_userinfo_200_with_a_non_object_body_redirects_and_audits[not-json-at-all]`, `[recursion]` — 2 failed, 93 passed |

M18/M19 are the reason the decode legs exist: an `isinstance`-only implementation handles a JSON
array and still 500s on an HTML interstitial, and that implementation used to pass the whole suite.

Two further mutations of the new code, for completeness:

| Mutation | Now fails |
|---|---|
| **NARROW** — `except (ValueError, RecursionError)` → `except ValueError` | the six `[recursion]` fences of §8.1 — 6 failed, 89 passed |
| **NOFIELD** — revert every `_google_str_field` call to its shipped read | L12 × 5, L13 × 2, S11 × 4 — 11 failed, 84 passed |

### 8.6 Coverage added

| Fence | Pins |
|---|---|
| **U4** (new) | `json.loads` really raises `RecursionError` on a nested body — asserted `not isinstance(caught.value, ValueError)` — and the helper absorbs it into the `None` sentinel. `.json()` performs a **genuine** decode; stubbing an instance would fence the `except` tuple but not the claim its width rests on, and that claim is what was wrong |
| **U2** | `recursion-error` leg; exception legs converted to per-call factories |
| **U3** | `bad_access_token_type` for dict / int / list; `null` and `""` stay `no_access_token`; wrong-type word carries no value; first fixture corrected |
| **L6** | parametrized: `empty-token`, `number-token`, `object-token` beside the original OAuth-error body |
| **L7** | `recursion` leg; factories |
| **L8** | parametrized `json-array` / `not-json-at-all` / `recursion`; **wrapped in `patch.object(auth_module, "_LOGGER")`** with an exact field set and `phase == "userinfo"` |
| **L12** (new) | non-string email (`null`, list, number, object, bool) → `no_email` branch, exact `detail`, **and no `User` row created** |
| **L13** (new) | non-string `given_name` / `family_name` / `picture` → callback **succeeds**, field dropped, no audit row; driven on **both** the new-user and existing-user branches |
| **S6** | parametrized over all seven token-body shapes, re-asserting the four step-up pins on every leg; includes the `unusable_access_token` twin the login site had alone |
| **S7** | parametrized decode legs; `_LOGGER` patched, exact field set, `flow == "stepup"`, `phase == "userinfo"` |
| **S11** (new) | non-string email at the step-up site → `email_mismatch`, correct `event_type`/`actor_email`, **no step-up token minted** |

### 8.7 Invariants re-verified after the fold

- **`WRONG-BROAD`** (a new `except Exception:` per handler, given the *right* reason string)
  re-injected: **4 failed, 91 passed** — L5, L9, S5, S9 all `DID NOT RAISE _ProgrammerBug`.
  The anti-broadening property is intact. Restored.
- **L5 and S5 byte-unmodified**, verified by AST extraction and byte comparison against `HEAD`:
  `L5: BYTE-IDENTICAL (1169 bytes)` · `S5: BYTE-IDENTICAL (1857 bytes)`.
- **Zero new `except` clauses in either handler.** AST inventory of the fixed file:
  `google_callback: ['TimeoutError', 'httpx.HTTPError']`,
  `sso_stepup_callback: ['ValueError', 'TimeoutError', 'httpx.HTTPError']` — both byte-identical to
  `HEAD`. The only `except` line in the whole diff is the helper's own, widened to
  `_google_json_object: ['(ValueError, RecursionError)']`.

### 8.8 Also folded in

`_json_decode_error()` was evaluated **once at import**, inside `@pytest.mark.parametrize`, so one
mutable exception instance was shared and re-raised across every leg using it. The parametrize
tables now hold **factories** called per test, in both files. U3's redundant
`"refresh_token" not in detail` assertions were deliberately left: they express intent.
