# TBD-330 — Broadcast resume must not re-send what Mailgun already took

Status: agreed, ready to build
Date: 2026-08-07
Supersedes the ticket's own Definition of Done, which prescribes the wrong fix (see §8).

---

## 1. The defect, correctly located

The ticket says the bug is that the resume eligibility predicate never reads
`delivery_status`. That is a real gap, but it is **not** the proximate defect and
fixing only it leaves the headline scenario untouched.

`send_batch` (`backend/app/services/email_service.py:294-424`) returns a bare
boolean. It returns falsy for two epistemically opposite outcomes:

| outcome | did Mailgun get the message? |
|---|---|
| MA2 vars-mismatch pre-check (`email_service.py:348-360`) — no HTTP issued | **No.** Conclusive. |
| `raise_for_status()` on a 4xx (`:392`) | **No.** Mailgun parsed it and refused. Conclusive. |
| `except TimeoutError` — the 20s aggregate bound (`:400`) | **Unknown.** Request written, answer never read. |
| `except Exception` (`:412`) — `ConnectError` (no), `ReadTimeout` / `RemoteProtocolError` / 5xx (**unknown**) | mixed |

`broadcast_service.py:735` collapses all of them into
`"send_batch returned a falsy result"`, and `:743-753` writes
`status = FAILED`.

**`FAILED` is an assertion that Mailgun did not accept the message.
`ok=False` does not license that assertion.** A row written this way is
re-send-eligible and carries a permanently-NULL `delivery_status`, so no
reader-side predicate can rescue it.

### The repo already ruled on this state, and the code contradicts the ruling

`broadcast_service.py:324-329`, mirroring R2 in
`specs/2026-07-18-admin-email-broadcast-design.md:472-482`:

> a crash between the claim commit and a successful Mailgun receipt leaves an
> in-flight batch marked `sent` but possibly undelivered — it is **NOT** retried
> by `resume`.

A 20s aggregate timeout is informationally identical to a crash mid-call. The
crash path honours the ruling (rows stay `SENT`). The timeout path does not.
TBD-330 is that inconsistency.

### Provenance

The high-probability path was opened by **TBD-266 / PR #627 / `aa5c4dde`**,
merged 2026-08-06. It correctly bounded the send, then routed a *new* "unknown"
outcome into the pre-existing "failed" channel whose only consumer reads failed
as "definitely not sent".

`backend/tests/services/test_email_send_aggregate_timeout.py:181` pins the wrong
contract in its own docstring:

> "Same bound on the broadcast path, whose failure contract the drain already
> reads as *revert these rows to failed and let a resume retry*."

That sentence is the bug, recorded as intended behaviour, green in CI. This is
`reference_half_fix_leaves_a_door.md`, instance N+1.

### Why it is likely, not exotic

`broadcast_batch_size` defaults to **1000** (`backend/app/config.py:320`) and
`MAILGUN_SEND_TOTAL_TIMEOUT_S` is **20.0s** (`email_service.py:67`) for one POST
carrying 1000 addresses plus a `recipient-variables` blob plus the body. On
timeout: 1000 rows → `FAILED`, `delivery_status` NULL, operator clicks Resume,
1000 duplicates, up to `broadcast_max_attempts = 3` (`config.py:319`) — i.e.
**2 duplicates per recipient**, not 3 as the ticket claims.

---

## 2. The `delivery_status` value set — established, not assumed

Sole producer is `map_event`, `backend/app/services/mailgun_webhook.py:115-136`.
Sole writer is `backend/app/routers/webhooks.py:123`.

| Mailgun event | persisted value | line |
|---|---|---|
| `delivered` | `delivered` | `:127-128` |
| `failed` + `severity == "temporary"` | `bounced_temporary` | `:131-132` |
| `failed` + permanent/missing/unknown | `bounced_permanent` | `:133` |
| `complained` | `complained` | `:134-135` |
| everything else (`accepted`, `opened`, `clicked`, `unsubscribed`, `rejected`) | `None` → router 200-drops | `:136` |

**There is no `accepted` value.** The ticket's DoD names one.

**Every non-NULL value suppresses a re-send.** The "safe to re-send" set is
empty: `delivered` → duplicate; `bounced_temporary` → Mailgun is still retrying
internally, ours would stack a second message; `bounced_permanent` → fastest way
to get a sending domain throttled; `complained` → worst available action.

So the discriminator is **non-nullness**, not membership in a value subset.

---

## 3. The fix

### 3.1 Upstream (primary) — classify the send outcome

`send_batch` returns a **typed result object with attribute access**, not a
boolean and not a bare `Enum`.

**This is a hard requirement, for a stated reason.** Every `Enum` member is
truthy and every non-empty string is truthy, so a stale `if not ok:` call site
would silently treat a rejection as success. With attribute access, every stale
caller and every `AsyncMock(return_value=True)` raises `AttributeError` — the
migration fails **loud**, not silent.

Carry a tri-state classification:

- `ACCEPTED` — 2xx observed, and the dev-mode no-op (`email_service.py:362-366`)
- `REJECTED` — provably never queued: MA2 pre-check (`:348`), a 4xx, and any
  exception raised before the request was written (`ConnectError`,
  `ConnectTimeout`, `PoolTimeout`)
- `INDETERMINATE` — written-or-maybe-written, no conclusive answer: builtin
  `TimeoutError` from the aggregate bound, `ReadTimeout`, `WriteTimeout`,
  `RemoteProtocolError`, 5xx, and **any unrecognised exception**

⚠ **The classification detail an implementer will get wrong:** httpx's own
per-phase timeouts do **not** derive from builtin `TimeoutError`. `ReadTimeout`
lands in the generic handler at `:412` and is logged as `email_send_failed` with
`error_type="ReadTimeout"` — verified. Classifying by `except TimeoutError`
alone mis-buckets the most ambiguous case of all. Default-to-`INDETERMINATE` on
anything unrecognised; that is the fail-safe direction under the never-double-send
invariant.

### 3.2 The drain's post-send branch (`broadcast_service.py:743-753`)

- `ACCEPTED` → rows stay `SENT` (unchanged)
- `REJECTED` → revert `SENT → FAILED` with the specific reason (unchanged
  behaviour, now correctly scoped)
- `INDETERMINATE` → **rows stay `SENT`.** No revert. Record the specific reason
  in `error`, emit `broadcast_batch_indeterminate`.

**Delete** the generic `"send_batch returned a falsy result"` string (`:735`).
The typed reason replaces it, moving the timeout-vs-rejection distinction out of
structlog and into a queryable column.

### 3.3 The predicate — in TWO places, and the second is load-bearing

Term: `EmailBroadcastRecipient.delivery_status.is_(None)`

**(a) Resume eligibility** — hoisted into the shared `conditions` list
(`broadcast_service.py:614-617`) so it applies to both eligibility branches. It
is a no-op on the fresh-send path (a `PENDING` row was never claimed, so no
Mailgun message exists to report on); it ships there as an invariant and **no
fence may be written against it on that path** — it is vacuous by construction.

**(b) The claim UPDATE's `.where(...)` (`:694-698`)** — this is the one that
closes the door.

The SELECT (`:620-633`), the per-row segment re-check loop (`:641-658` — one
await plus a commit per row, up to 1000 round-trips) and the claim (`:692`) are
separated by real time, during which the webhook sink accepts events at up to
300/min. A SELECT-only fix is internally consistent, passes every naive fence,
and leaves the door open.

**The existing rowcount-mismatch guard does not save a SELECT-only fix.**
Without the term on the claim, the UPDATE matches every `survivor_id` regardless
of `delivery_status`, so `claimed == len(survivor_ids)`, the guard at `:712-721`
never fires, and the just-delivered row is mailed.

⚠ **Liveness cost, must be documented in code and named in the PR:** with the
term on the claim, one late webhook makes `claimed != len(survivor_ids)`, so the
guard rolls back and `continue`s — **skipping the entire batch** (up to 1000
recipients) to a later resume click. Those ids are already in `seen_ids`
(`:642`) so they are excluded for the rest of that run. This is safe
(never-double-send holds) and consistent with existing claim-mismatch semantics,
but the next operator will file it as a new bug unless it is written down.

**Use `is_(None)`, never `== None`** (ruff E711).

### 3.4 Two traps this exact form avoids

1. **SQL three-valued logic.** `delivery_status.notin_(["delivered", ...])`
   evaluates to `NULL` for a NULL row → the row is excluded → **resume sends to
   nobody**, which passes any naive "no duplicates" assertion. This is the most
   likely wrong implementation and fence F4(b) exists for it.
2. **MySQL collation.** `delivery_status` is `VARCHAR(32)` under
   `utf8mb4_0900_ai_ci`. A value comparison is case-insensitive on MySQL and
   case-sensitive on the SQLite test DB — the TBD-322 class, invisible to CI.
   `IS NULL` is collation-independent.

### 3.5 Observability

Emit `broadcast_batch_indeterminate` at **`error`** level (not `info` —
`broadcast_batch_sent` is info, and an unresolved delivery question must not sort
with successes). Carry `broadcast_id` and `count` so the size of the uncertainty
is legible. **Honour MA5: no addresses, no bodies.**

This is not redundant with the typed `error` column: under this design an
INDETERMINATE batch changes no status, no counter and no UI element — it looks
exactly like a clean send, so the operator has no trigger to go looking. The
existing `broadcast_batch_timeout` (`email_service.py:404`) names the *transport*
failure, not the drain's decision to keep rows claimed and unretryable.

Additionally: add `error` to `RecipientResponse`
(`backend/app/schemas/email_broadcast.py:100-106`), which currently omits it, so
the typed reason can actually reach the operator. **API field only — do not add
a UI column.** A new column in the recipients table is a design change and needs
the operator's visual approval; it is a follow-up, not this PR.

---

## 4. RULING: no reconcile UPDATE

Both architects changed position on this and **crossed** — A moved to "do not
reconcile", B moved to "do reconcile". Neither held it strongly. Settled here on
the one piece of evidence neither contested.

**Do NOT write back a corrected `status` for a `FAILED` + non-NULL
`delivery_status` row.** Reasons, in order of weight:

1. **The population is verifiably zero today.**
   `MAILGUN_WEBHOOK_SIGNING_KEY` was set 2026-07-21 (`.do/app.yaml:180-182`), and
   `mailgun_webhook.py:60-63` **fails closed** with no key, so the endpoint 404s
   before that date. The single production broadcast went out 2026-07-20 with 19
   recipients. Those rows carry a permanently-NULL `delivery_status`. There is
   nothing to reconcile.
2. **It is closed for future rows.** After §3.2, `status = FAILED` requires an
   *answered rejection* — Mailgun gave a definitive non-2xx, so nothing was
   accepted, so no webhook can fire for that row. The only residual is a
   proxy-level 5xx arriving after Mailgun enqueued, which is not a normal shape
   for an accept-or-refuse API.
3. **It weakens the invariant the design relies on.** Both architects prove
   race-freedom by citing the **disjoint column sets**: the drain writes
   `status`/`attempts`/`sent_at`/`error`; `_apply_delivery_status` writes
   `delivery_status`/`delivery_updated_at`. A reconcile adds a *third* writer to
   `status`, and puts an N-row `UPDATE ... SET status` into lock contention with
   the webhook's `SELECT ... FOR UPDATE` (`webhooks.py:93`).
4. **Its predicate has a subtle trap both architects tangled in.**
   `WHERE delivery_status IS NOT NULL` includes `bounced_permanent` and
   `complained`. Whether promoting those to `sent` is correct turns on a fine
   reading of R1, and the fact that two rounds could not settle it is itself
   evidence the mechanism is the wrong shape for a zero-row problem.

**Ship instead:** a code-comment invariant at the drain's claim/revert block and
referenced from `_apply_delivery_status`:

> `status` / `attempts` / `sent_at` / `error` are the **handoff** record and are
> written ONLY by the drain. `delivery_status` / `delivery_updated_at` are the
> **outcome** record and are written ONLY by `_apply_delivery_status`.
> `status='failed'` with a non-NULL `delivery_status` is left as-is: it is a
> near-impossible state, and reconciling it would add a third writer to `status`.
> Do not add one.

Fence F7 pins the invariant in **both** directions (§5).

**Reviewers: attack this ruling.** If you can produce a concrete row set, an
operator action, and a wrong number the operator actually sees, the ruling
flips.

---

## 5. Fences

Standing rules for every fence below:

- **All three legs plus restore:** RED before → green after → RED against the
  named mutant → **green again on restore**. Back files up to
  `/Users/flamarion/.claude/jobs/460e17e5/tmp/` first; **never `git checkout --`**
  (`reference_git_checkout_wrong_restore_primitive.md`).
- **Every fence asserting "X was not sent" MUST assert "Y WAS sent" in the same
  test.** No exceptions.
- **Assert the exact address SET in `to_list`**, never `await_count` alone —
  the count is identical for "resumed everybody" and "resumed the right one".
  Existing idiom: `test_broadcast_drain.py:279`, `:379`.
- Isolated compose project, `-p team-330` on **every** command.

| # | Fence | Fixture (must make correct and incorrect disagree) | Named mutant |
|---|---|---|---|
| **F1** | `send_batch` classifies three outcomes — in `test_email_send_aggregate_timeout.py` | 2xx + dev-mode → ACCEPTED; MA2 mismatch + 4xx → REJECTED; `TimeoutError` + `ReadTimeout` → INDETERMINATE | (a) `except TimeoutError` returns REJECTED → RED. (b) **the other side of the boundary**: classify the 4xx as INDETERMINATE → RED. A boundary pinned from one side is not pinned. |
| **F2** | An indeterminate batch leaves rows `SENT` and resume does not touch them | 3 `PENDING`; mock → INDETERMINATE; assert all `SENT`, `attempts==1`; then `resume_pending` with a fresh mock, assert `await_count == 0` | widen the revert guard to `if outcome is not ACCEPTED:` → RED. **Must be RED against unmodified `main`** — if green on main it is vacuous. Also RED against a predicate-only fix, which is the point. |
| **F3** | A conclusively-rejected batch still reverts AND still resumes — **mandatory control** | 3 `PENDING`; mock → REJECTED; assert all `FAILED`; then resume with mock → ACCEPTED, assert `set(to_list)` is all three | REJECTED handler leaves rows `SENT` → RED. **Kills "never revert anything"**, which passes F2. ⚠ Label explicitly: F3 is **green against unmodified `main` by design** — it is a control, not a coverage claim. |
| **F4** | NULL is re-sent, non-NULL is not, in ONE call | 4 rows all `FAILED, attempts=1`: r0 `None`, r1 `delivered`, r2 `complained`, r3 `bounced_permanent`. Assert `set(to_list) == {r0.email}` exactly | (a) delete the `is_(None)` term → RED. (b) **the important one** — replace with `.notin_([...])` → RED **because `to_list` is empty** (NULL propagation). (c) `.isnot(None)` → RED. r0's presence is the DoD-4 control. |
| **F5** | Claim refuses a row that gained `delivery_status` mid-flight — **the half-fix killer** | hook `_user_still_targetable` (`:501-520`) to write `delivery_status='delivered'` on r0 between SELECT and claim; 2 rows | remove the term from the **claim UPDATE only**, leaving it in the SELECT. F1–F4 all stay green; only F5 goes RED. Assert `send_batch.await_count == 0`, `broadcast_batch_claim_mismatch` logged, r1 still eligible. |
| **F6** | `delivery_status` value set is closed | assert `{map_event(e,s)} - {None} == set(DELIVERY_RANK) - {None}` | add a fifth mapping to `map_event` → RED. ⚠ An `== exact set` assertion is a known repo hazard (`reference_reports_v3_networth_source`); it is deliberate here, and **its docstring must name the TBD-330 resume predicate** so widening the map forces a re-read. |
| **F7** | The `FAILED` + `delivered` row is left untouched — pins §4 in both directions | r0 `FAILED` + `delivery_status='delivered'`; r1 `FAILED` + NULL | (a) delete the `is_(None)` term → RED (r0 re-sent). (b) **add a reconcile UPDATE** → RED (r0's `status` changed). Assert r0 not in `to_list`, `status` still `FAILED`, `delivery_status` still `delivered`. |
| **F8** | `broadcast_batch_indeterminate` is emitted at `error` with `broadcast_id` + `count`, and carries **no addresses** | fold into F2 | drop the log call → RED; log at `info` → RED. |
| *(G1)* | Fresh drain still sends NULL-status `PENDING` rows | — | **Labelled a regression guard, NOT a fence.** Vacuous by construction (§3.3a). Must be documented as such so nobody counts it as coverage. |
| *(G2)* | Attempts cap unaffected | `test_resume_does_not_rebatch_failed_at_cap` (`:357`), `test_resume_leaves_pending_at_cap_and_stays_sending` (`:388`) stay green with `delivery_status=None` seeded explicitly | regression guard |

### Pre-existing tests that MUST change

`test_broadcast_drain.py:240` (`test_drain_failed_batch_then_resume`) and
`test_email_send_aggregate_timeout.py:181` / `:306` encode the boolean contract.

⚠ **If either passes unchanged, the upstream fix was not made** and what shipped
is the predicate-only half-fix. Treat "these still pass" as a build failure, not
a convenience.

### Vacuity self-check before claiming done

Revert `broadcast_service.py` and `email_service.py` to `main` and confirm F1,
F2, F4 (all three mutants), F5 and F7 are **RED**, *and that the pre-existing
drain tests are GREEN in that state*. If the pre-existing tests also go red, the
source never loaded and the greens prove nothing
(`reference_sprint7_vacuity_classes.md`).

---

## 6. Explicitly NOT in scope

- **No new column. No migration. No new `RecipientStatus` member.** `status` is a
  **native MySQL enum** (`models/email_broadcast.py:130-139`), unlike
  `delivery_status`; widening it is the ALTER-ENUM landmine that is green on
  SQLite CI and 500s on prod (`reference_abn_tab_import.md`).
- **No grace window, no wall-clock comparison.** `sent_at` is set at *claim*
  time (`:703`) and not cleared by the revert, so it timestamps the wrong event
  and cannot answer "did the attempt complete". A time-based rule would also make
  the same Resume click behave differently at 10:00 and 10:31.
- **No UI column for `error`.** API field only; the column is a design change
  needing operator visual approval.

---

## 7. Known residual, accepted and named

Rows *already* `FAILED` in the database under the old lossy contract, whose
webhook has not arrived, are indistinguishable from legitimately-rejected rows —
`error` is the same generic string for both. The predicate cannot classify them.

Accepted because: it is **closed for all future rows**, and the production set is
**zero** (§4.1). Named here rather than claimed not to exist.

---

## 8. Where the ticket's DoD is wrong

1. **DoD 1 names a value that does not exist.** There is no `accepted` in
   `delivery_status`. An implementation matching on it matches nothing, forever,
   silently.
2. **DoD 1's implied dichotomy is backwards for two values.** "Which mean it
   definitely did not land (safe to re-send)" would classify `bounced_permanent`
   and `complained` as re-sendable — the two values where re-sending is *most*
   harmful. There is no safe-to-re-send value.
3. **The DoD does not fix its own headline scenario.** The dominant case is the
   20s timeout on a 1000-address batch, which produces `FAILED` rows with
   `delivery_status` **NULL**. "Exclude recipients whose `delivery_status` shows
   delivered" fires on none of them.
4. **The actual defect is unnamed anywhere in the DoD** — `send_batch`'s boolean
   conflating "rejected" with "unknown", and `:743-753` promoting "unknown" to
   "definitely not sent".
5. **DoD 2's premise is unsettled and the answer is "do nothing"** (§4). It says
   the row "should probably be reconciled"; "probably" is not a spec.
6. **DoD 3 asks the right question and the answer is that the question
   dissolves.** Ruling on the NULL window by predicate is impossible because no
   column records whether the attempt completed. §3.2 empties the ambiguous
   population instead.
7. **DoD 4's control is necessary but not sufficient.** "A genuinely-undelivered
   recipient IS still re-sent" is satisfied by an all-NULL fixture, which cannot
   distinguish the correct predicate from the polarity-flipped one. The control
   must sit in a **mixed** fixture and assert the exact `to_list` set (F4).
8. **Blast radius overstated:** 3 sends total means **2 duplicates**, not 3.
9. **The ticket never mentions the claim UPDATE**, which is where the door
   actually closes (§3.3b).
10. **Unverified assumption:** the ticket assumes the Mailgun delivery webhooks
    are registered on the sending domain. The signing key is set, but subscribing
    the events is a separate console action the repo cannot see. **If they are
    not registered, every reader-side term here is inert in production** — a
    second, independent reason the primary fix is upstream. Operator to confirm.

## 9. Adjacent defect, file separately

`POST /{id}/resume` (`backend/app/routers/admin_broadcasts.py:619-654`) has **no
broadcast-status gate** — it resumes a `completed` or `failed` broadcast as
happily as a `sending` one. That is what makes this bug reachable from the UI
indefinitely, on any past broadcast. Out of scope here.
