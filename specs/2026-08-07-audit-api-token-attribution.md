# TBD-188 — Record the acting PAT on audit events

Status: agreed, ready to build
Date: 2026-08-07

Two independent architects; B verified A's path analysis branch by branch, found no
counterexample, and withdrew its own competing mechanism. §8 records where the ticket
is wrong.

---

## 1. What is actually missing

The ticket says *"audit events triggered by a PAT-authenticated request do not record
which token was used."* **That is false for three event types** — `api_token.auth_rejected`
(`backend/app/auth/pat.py:79-83`), `api_token.created` (`backend/app/routers/api_tokens.py:228`)
and `api_token.revoked` (`:328`) already put the id in `detail`.

The real gap is **ordinary actions performed via a PAT**. And the reason a column beats
the status quo is not convenience:

- `.do/app.yaml` declares **no `log_destinations`**, so the v1 forensic path
  (`request_id` ↔ structlog join) terminates in DO's short-retention, non-queryable
  buffer. `backend/app/models/audit_event.py:1-7` says the table exists precisely
  because structlog "isn't a queryable history with SLA-grade retention."
- ⚠ **`detail.api_token_id` is semantically overloaded and cannot be extended.** At
  `pat.py:80` it means the **acting** token; at `api_tokens.py:228`/`:328` it means the
  **subject** token, and those two routes are `require_interactive_session`, so the actor
  there is a **JWT session**. A query for "everything token 42 did" over `detail` would
  return the row where token 42 was *revoked by a human*. A separate column is what keeps
  actor and subject apart.

## 2. Mechanism: read the contextvar inside `_build_audit_event`. No new parameter.

Both public entry points — `record_audit_event(session_factory, ...)`
(`backend/app/services/audit_service.py:77`) and `add_audit_event_to_session(session, ...)`
(`:152`) — delegate to `_build_audit_event()` (`:52`, called at `:118` and `:174`).
**One read there covers all 108 call sites with zero call-site churn.**

Rejected, with reasons:

- **`request.state`** — unreachable from a service function without threading a
  `Request`. **22 of the 108 sites are in `app/services/` and have no `Request` at all.**
  It is also the dead channel: `request.state.api_token_id` (`pat.py:184`) has **zero
  readers** anywhere in `app/` or `tests/`.
- **A threaded kwarg at 108 sites** — 108 individually-forgettable edits whose failure
  mode is silent: a kwarg defaults to `None`, so any site that forgets it is permanently
  NULL with CI green, and a new audit site added in six months is NULL by default. The
  contextvar read inverts that: a new call site is **correct by construction**.

**On the coupling objection.** `audit_events.request_id` is already resolved exactly this
way at ~85 of 108 sites, each doing `structlog.contextvars.get_contextvars().get(...)`
inline. This design does not introduce ambient reads; it moves one inward. The
preconditions are established: `RequestContextMiddleware` is **pure ASGI**
(`backend/app/middleware/request_context.py:80-99`) specifically so handler-bound
contextvars stay visible, and it calls `clear_contextvars()` per request at `:103`.

## 3. ⚠ THE HALF-FIX DOOR — fix the binder, do not add an escape hatch

`_record_auth_rejected` fires at `pat.py:126` and `:130`, **before** the bind at
`:188-193`. A naive contextvar implementation therefore leaves `api_token.auth_rejected`
— the one event type entirely *about* a token — with `api_token_id IS NULL`, while
`detail` still carries the id so the row *looks* attributed.

**The defect is not "the builder cannot be told." The binder binds too late.**

Lift `api_token_id=row.id` out of the composite bind into its own call placed
immediately after the `row is None` guard:

```python
row = await lookup_token(db, raw_token)
if row is None:
    logger.info("pat.auth_rejected", reason="unknown")
    raise _generic_401()

# Bind BEFORE the validity checks: every path below either writes an
# api_token.auth_rejected row (which must carry this id) or raises without
# writing anything. See audit_service._build_audit_event.
structlog.contextvars.bind_contextvars(api_token_id=row.id)
```

**Safety proof — every path after the new bind point, exhaustive and independently
re-verified by both architects:**

| `pat.py` | path | audits? | wants the id? |
|---|---|---|---|
| `:124` | revoked → `_record_auth_rejected` | yes | yes |
| `:128` | expired → `_record_auth_rejected` | yes | yes |
| `:134` | `created_by_user_id is None` | no, bare `raise` | n/a |
| `:142` | owner row missing | no, bare `raise` | n/a |
| `:148` | inactive / not-superadmin | no, bare `raise` | n/a |
| `:161-178` | scope mismatch (3 exits) | no, bare `raise` 403 | n/a |
| `:183+` | success | downstream handler does | yes |

**No path binds a token id and then audits a non-token action**, because every
non-success path terminates by raising out of `get_current_user`. `authenticate_pat` has
exactly one caller (`deps.py:41`, which returns it directly), and
`get_current_user_optional` (`deps.py:95-134`) has **no `pat_` branch** — a `pat_` bearer
there falls through to `decode_token`, fails, and returns `None`.

**Semantic to write into the model docstring:**

> `audit_events.api_token_id` = the API token **presented as the credential** for the
> request that produced this row. On `outcome="success"` rows it additionally validated;
> on `api_token.auth_rejected` rows it was presented and rejected.

**Security: this is a triage improvement, not an exposure.** The token **id** is already
logged at `pat.py:125, 129, 135, 143, 152` for every rejection reason. `id` and
`token_prefix` are non-secret; `token_hash` is the secret and is never bound. The only
genuinely new coverage is the scope-mismatch 403 (`:161-178`), which today logs
**nothing at all** — a 403 with zero attribution is a real blind spot in exactly the
leaked-token hunt this ticket serves.

## 4. Delete the dead channel

**Delete `request.state.api_token_id` (`pat.py:184`).** Zero readers. Once
`_build_audit_event` reads the contextvar, leaving it is a second channel carrying one
fact — how a divergent reader gets written next quarter. **Keep
`request.state.auth_method`**; it has a real reader (`require_interactive_session`,
`pat.py:217`).

⚠ This deviates from `specs/2026-07-21-superadmin-api-tokens-design.md:204`, which
prescribes "shared helper reading `request.state`" — i.e. it prescribes reading the
channel nobody reads. **Correct that spec line in this PR**, or the next agent
implements the deprecated design straight from the authoritative document.

## 5. Scope: API-complete, UI-none

| | ruling |
|---|---|
| Column + model + migration | **YES** |
| `api_token_id` filter in `list_audit_events` | **YES** — the query the index exists for |
| `api_token_id` query param on `GET /api/v1/admin/audit` | **YES**, mirroring `actor_user_id`'s `ge=1` |
| `api_token_id` on `AuditEventResponse` | **YES** |
| `_SORTABLE["api_token_id"]` | **NO** — you filter by a token id, never order by one. `_SORTABLE`'s keys are the frontend's sort tokens, so an entry with no `SortableHeader` is dead surface on a closed whitelist. **Fenced: `?sort_by=api_token_id` must 400.** |
| Frontend type + UI filter input + table column | **NO** — filed as **TBD-349**, which needs the operator's visual approval |

A write-only column would leave the operator no better off than today and would
*foreclose* the follow-up by making the ticket look done.

## 6. Migration

`backend/alembic/versions/079_audit_api_token_id.py`, `revision = "079_audit_api_token_id"`
(21 chars, under the CI-enforced 32 — `backend/tests/test_alembic_revision_id_length.py:40`),
`down_revision = "078_recurring_occurrence_count"`.

```python
sa.Column("api_token_id",
          sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
          nullable=True)
```

⚠ **`BigInteger` is load-bearing.** `api_tokens.id` is
`BigInteger().with_variant(Integer, "sqlite")` (`backend/app/models/api_token.py:46-50`)
→ `BIGINT` on MySQL, and MySQL rejects an FK whose column type differs. A plain
`sa.Integer()` is green on SQLite CI and fails at `ALTER TABLE` on prod — the
`reference_abn_tab_import` landmine in a new dress. The `with_variant` is *stylistic* on
a non-PK column in SQLite (same integer affinity) but must not be "simplified" away.

**FK `ON DELETE SET NULL`, and index — both required.** MySQL requires an index on an
FK's referencing column and will auto-create one with an unpredictable name, so create
`ix_audit_events_api_token_id` **first** and let the FK adopt it. Both existing
`audit_events` FKs are `SET NULL` for the reason documented at `audit_event.py:21-27`:
audit history must outlive the rows it describes. Tokens are soft-revoked
(`api_token.py:78`) and never hard-deleted, so the branch never fires — which is exactly
why it is free.

⚠ **`downgrade()` order is this migration's one landmine and is invisible on SQLite:**
drop FK → drop index → drop column. MySQL InnoDB errno 1553 rejects dropping an index
still covering an FK (`reference_mysql_fk_index_cover`).

**No backfill, and this is a "must not", not a "cannot".** Of the three events carrying
`detail.api_token_id`, **two must be NULL** (§7 class 4) — a naive backfill would write
the *subject* into an *actor* column and permanently corrupt the semantic. Record the
cutover in the migration docstring so an operator reading a NULL knows whether it is
informative.

## 7. The complete NULL set — six populations, not two

| # | population | value | mechanism |
|---|---|---|---|
| 1 | PAT-authed HTTP, post-bind | **token id** | contextvar |
| 2 | Interactive JWT | NULL | `deps.py:81` never binds it |
| 3 | **PAT rejected pre-bind** (`api_token.auth_rejected`) | **token id** | §3's relocated bind. **Naive impl → NULL. This is the door.** |
| 4 | **`api_token.created` / `revoked`** | **NULL** | actor is a JWT session; the subject id stays in `detail` |
| 5 | Pre-auth / anonymous (14 of 108 sites in `routers/auth.py`) | NULL | no auth context bound |
| 6 | Scheduler / system (`services/scheduler/audit.py`) | NULL | lifespan-spawned task, empty context snapshot |

⚠ **Class 4 is the highest-risk.** The wrong intuition — "the row mentions a token, so
set the column" — is the single most likely thing a future implementer or reviewer will
act on, and acting on it permanently mixes actor and subject in one field.

⚠ **Class 6 is structural, not policy.** Do **not** add explicit `api_token_id=None` at
the scheduler sites: the state it would guard (scheduler running inside a request
context) is unreachable, and a fence against it is an unreachable-predicate test.

## 8. Fences

⚠ **All three traps below are live. A fence that misses any of them is decoration.**

- **Trap 1 — the factory.** `backend/tests/factories/app.py` hard-codes
  `request.state.auth_method = "jwt"` in **both** resolver branches (`:138-147` and
  `:164-172`), so `authenticate_pat` never runs. **The non-NULL fences must NOT use
  `make_test_app`** — use the `tests/auth/test_pat_authentication.py::_make_client` shape
  (bare `FastAPI()`, real `get_current_user`, real `pat_` bearer).
- **Trap 2 — the hand-bound contextvar.** A test that calls `bind_contextvars` itself
  proves only that the builder *reads*; it is green against an implementation where
  `authenticate_pat` never *binds*. Confine manual binds to the builder unit test.
- **Trap 3 — the false null.** A PAT aimed at an interactive-gated route gets 403 and
  writes **no audit row**; `row is None` then satisfies a NULL assertion for entirely the
  wrong reason. **Every NULL fence must additionally assert the row EXISTS.**

**Acceptance route: `POST /api/v1/tags`** (`backend/app/routers/tags.py:77-113`). Proven
to qualify: absent from `INTERACTIVE_ONLY_ROUTES`; gated only by
`Depends(get_current_user)` (`tags.py:81`); `POST ∈ _WRITE_METHODS` so it exercises the
`scope == "write"` branch (`pat.py:161-166`); audit write is unconditional on success and
uses `record_audit_event`; `detail` carries no token id, so a green cannot be satisfied
by the pre-existing writes. **The NULL leg uses the SAME route with a JWT bearer** — the
credential is then the only varying input.

| # | Fence | Named mutant |
|---|---|---|
| **F1** | Pop 1: write-scope PAT → `POST /tags` → 201 → `tag.created` has `api_token_id == token.id` | **M1a** `audit_service` — replace the contextvar read with `None`. **M1b** `pat.py:192` — delete the bind. Both RED. Test must never call `bind_contextvars`, and must assert `get_contextvars().get("api_token_id") is None` before the request. |
| **F2** | Pop 2: JWT, same route, same app → row exists, `api_token_id IS NULL` | **M2** `audit_service` — `ctx.get("api_token_id") or ctx.get("user_id")` (the DoD's "inventing a value"). ⚠ Must also assert the row exists and `actor_user_id == user.id` (trap 3). |
| **F3** | **Pop 3 — THE DOOR.** revoked token → 401 → `api_token.auth_rejected` has `api_token_id == token.id` **in the column** AND `detail["api_token_id"]` still set | **M3 — POSITIONAL, not value-based:** move the bind from its new position back into the composite bind at `:188-193`. **F1 stays GREEN; F3 must go RED.** ⚠ A reviewer's instinct is to mutate the *value* (`row.id` → `None`), which reddens for the wrong reason and pins nothing. Under this design the bind's **line position** is the entire fix and nothing in the code enforces it — say so in a comment at the bind site. Parametrize over the **expired** branch (`:128`) too; it is a separate call site. |
| **F4** | Pop 4: `POST /api/v1/system/api-tokens` with a superadmin **JWT** → `api_token.created` has `api_token_id IS NULL` while `detail["api_token_id"]` == the new token id. Repeat for revoke. | **M4** — insert `bind_contextvars(api_token_id=row.id)` before the `record_audit_event` at `api_tokens.py:228`. RED. |
| **F5** | Builder unit: no ctx → NULL; ctx bound → that id | **M1a**. The only fence permitted to bind manually. |
| **F6** | API filter round-trip: `GET /admin/audit?api_token_id=1` → `total == 1` and `items[0]["api_token_id"] == 1` | ⚠ Fixture needs **three** rows — `api_token_id=1`, `=2`, and **NULL**. With one non-null row an unfiltered implementation returns 2 and the failure is ambiguous. **M6a** delete the `where` clause (RED on `total`); **M6b** delete the schema field (RED on the field, **not** on `total` — which is why both assertions are required). Plus `?api_token_id=0` → 422. |
| **F7** | Source guard: `bind_contextvars(api_token_id=...)` occurs in **exactly one file, once** | AST walk in the style of `tests/test_no_raw_request_client.py`. **M7** — add a second binder anywhere under `app/`. RED. This is what keeps §3's "one binder" answer true rather than documented. |
| **F8** | `?sort_by=api_token_id` → 400 `invalid_sort_by` | Pins §5's `_SORTABLE` exclusion so a later "helpful" addition is deliberate. |

**Required deliverable, not fenced:** an autouse `clear_contextvars()` fixture (before
and after each test) in `backend/tests/conftest.py`. Two sync tests currently bind on the
main thread (`test_log_field_propagation.py:68,98`, `test_request_context.py:208`) and
clear only by convention. Fencing test-infrastructure ordering produces order-dependent
tests, which is worse than the defect — so this ships as a fixture with a comment, not a test.

⚠ **Do not fence "no cross-request bleed" through `TestClient`.** It drives the app via an
anyio portal in a separate thread whose context starts empty, so such a test passes even
against an implementation with zero clearing. If written at all, it must be run against
its mutant **first** and deleted if the mutant survives.

**Every fence: RED before → green after → RED against the named mutant → green again on
restore.** Back up to `/tmp` before injecting; **never `git checkout --`**. ⚠ Prove each
mutation was actually applied with a precondition and a postcondition that a *comment*
mentioning the same identifier cannot satisfy — a harness whose apply-step fails silently
produces a false "mutant survived" indistinguishable from real coverage.

## 9. Where the ticket is wrong

1. **"Do not record which token was used" is false for three event types** (§1). Left
   uncorrected, an implementer may "fix" the two where NULL is *correct*.
2. **effort-s is wrong in the dangerous direction.** Under an effort-s budget the thing
   that gets cut is the read path, and you ship a write-only column that looks done. The
   label will produce the half-fix. Re-estimate to M.
3. **The DoD names two populations; there are six** (§7). A DoD naming two will be signed
   off by a fence covering two.
4. **The DoD's null clause is satisfied by doing nothing.** A new nullable column is NULL
   everywhere — which is why F2 needs both a mutant and a same-run non-NULL sibling.
5. **The DoD blesses the door.** "Interactive-session requests leave it null" is satisfied
   *literally* by an implementation that leaves `api_token.auth_rejected` NULL.
6. **"Traceable" is the summary's word and is absent from the DoD** (§5).
7. **The authoritative spec prescribes the wrong mechanism** (§4) and must be corrected here.
8. **The ticket's three "Notes" are mostly red herrings.** `target_org_id` is correct but
   irrelevant; the `MissingGreenlet` snapshot pattern does not apply (contextvars are not
   ORM objects); the client-IP AST guard watches `request.client`, not `request.state`.
