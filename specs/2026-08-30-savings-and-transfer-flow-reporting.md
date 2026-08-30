# Savings and transfer-flow reporting — design spike (TBD-384)

**Status:** ruled. No implementation in this ticket.
**Date:** 2026-08-30. Measured against `main` @ `451cdb87`.
**Method:** two independent architects, opposed positions, one concede-or-defend
cross. Every fact below was re-verified against the code by the coordinator;
where an architect's claim did not survive that check it is marked.

---

## TL;DR

The data model is **not** the problem. The operator's first question is
answerable from data that already exists. Three things stand in the way, and
only one of them is what the ticket describes:

1. A shipped Reports control, **Type = Transfer, has never matched a single row**
   and never can. It returns an empty chart with no error, for every org, and
   has done since it shipped.
2. The transactions report source **sums across currencies silently**.
3. `non_reverted_transaction_filter()` **omits the `matched` state**, so the
   shipped "Include transfers & adjustments" toggle can double-count
   reconcile-matched duplicates.

The operator's *second* question — "how much of that was round-ups" — is not a
reporting question at all and is **cut from this ticket**.

---

## Q1 — Is the destination account type enough, or is an explicit intent needed?

**Ruling: filter on `account_type_id`. Never on `AccountType.slug`. No intent
column.**

`slug` is not a contract. `backend/app/routers/account_types.py:49` constructs
`AccountType(org_id=..., name=body.name)` and **never assigns `slug`**, so every
user-created type has `slug IS NULL`. An org that makes "High-Yield Savings" and
moves its savings account onto it gets `0.00` from a slug-keyed report —
silently, indistinguishable from "you saved nothing".

System types cannot be renamed or deleted out from under a slug
(`routers/account_types.py:71-75`, `:105-109` both 409 on `is_system`), so slug
*mutation* is not the hazard. **Absence is.**

⚠ `payment_source_service.py:45`'s `PAYMENT_SOURCE_ALLOWED_SLUGS` is **not** a
precedent for this. It is a **fail-closed safety denial**: an unknown or NULL
slug is refused loudly at write time with
`422 "Payment source must be a checking, savings, or cash account"` (`:98-101`).
A slug-keyed *money aggregate* fails the opposite way — silently, at read time,
returning a beautiful chart of nothing. Same predicate, inverted failure mode.

The repo has already ruled this, in writing, in the very tile that renders the
word "Savings" — `frontend/components/dashboard/widgets/BalancesByTypeTile.tsx:11-13`:

> Group by `account_type_id` (NOT a hardcoded slug allowlist) so accounts on
> custom/non-system types (`account_type_slug === null`) are included; a slug
> allowlist would silently drop their balances.

The backend agrees: `reports/sources/accounts.py:122-127` filters on
`Account.account_type_id.in_(value)` and only *labels* with `AccountType.name`.
Any design reaching for `slug == "savings"` reverses a settled ruling and
reintroduces the exact defect that ruling names. This is the
`reference_code_property_used_as_data_property` shape.

**No `intent` / `reason` / `provenance` column on `transactions`.** It would ship
100% NULL with zero writers, force a NULL branch into every consumer, and buy
nothing later that adding it later does not also buy — a column added today
backfills exactly as much history as one added in six months: none.

**Fence, and its vacuity trap:** create a **user-created** account type
(`slug IS NULL`), hang a savings account off it, assert a non-zero result. A
slug-keyed implementation returns `0.00`. ⚠ A fence built on the *seeded*
`savings` type passes under both implementations and proves nothing.

---

## Q2 — Round-ups

**Ruling: cut from TBD-384 entirely. File as its own product ticket.**

There is no round-up provenance because **there is no round-up producer**. A
whole-repo grep for `round.?up|roundup|spare.?change` returns two hits, both
unrelated arithmetic (`specs/ai-forecast-refine-cost-confirmed.md:140`,
`backend/tests/services/test_ai_pricing.py:75`).

So this is not "provenance we failed to store". Nothing has ever generated a
round-up transaction, there is no history to reconstruct, and no schema change
makes historical round-ups appear. It is a request for a round-up **engine**,
plus attribution on top — an order of magnitude larger than this spike, with a
one-way schema door.

If it is ever built, the attribution mechanism is most likely the existing tag
system (`TransactionTag` is already a published report dimension and filter,
`reports/sources/transactions.py:26,45`), **not** a new column. Recorded here so
the future ticket does not default to a column.

---

## Q3 — New source, new measure, or something else?

**Ruling: none of those. An orthogonal query *mode* on the existing
transactions source. No new `Dataset`, no new `MeasureField`, no new source
module.**

Verified: the **only** exact-set assertion over these enums is on `Dataset`
(`backend/tests/schemas/test_reports_enums_consistency.py:14`). `FilterField`
carries no such fence, and `FilterField.ACCOUNT_TYPE` / `FilterField.CURRENCY`
plus `Dimension.ACCOUNT_TYPE` / `Dimension.CURRENCY` **already exist**
(`schemas/reports_query.py:85-86`). They are simply not published by the
transactions source (`reports/sources/transactions.py:38-46`).

So the capability needs **no enum widening**, and a new `Dataset.SAVINGS_FLOW`
is rejected: it would trip that fence, trigger the security review its docstring
demands, and buy nothing the mode does not.

A new `MeasureField.SAVINGS` is also rejected. `sum_amount` already means the
right thing; there is nothing "savings" about the arithmetic. The savings-ness
lives entirely in the WHERE clause.

### ⚠ Why transfer-ness must be a MODE and not a `txn_type` value

This is the load-bearing ruling of the spike, and it was the one point that
survived the cross intact.

`txn_type` is published with ops `("eq","in")`
(`reports/sources/transactions.py:43`) and rendered as an **OR checkbox group**
(`FilterEditor.tsx:277-286`). There is no AND across values of one field.

So if "transfer" is a *value* of `txn_type`:

- `[transfer]` alone cannot express direction.
- `[transfer, income]` **widens** the result rather than narrowing it — it ORs
  in salary and interest.

**"Is a transfer leg AND inbound" becomes inexpressible.** That is precisely
the operator's question. Putting transfer-ness on its own axis makes
`mode = transfers_only` compose with `txn_type = income`.

### The €700 problem, verified

`Transaction.amount` is `Field(gt=0, ...)` (`schemas/transaction.py:16,50`) —
magnitude only, direction carried by `type`. And the measure is a bare sum:
`SourceMeasure("sum_amount", "Total amount", "sum", "amount", "currency")`
(`reports/sources/transactions.py:33`), compiled by
`reports_query_service._measure_expr` as `func.coalesce(func.sum(col), 0)` with
**no `CASE` on type** (contrast `routers/settings.py:_roster_settled_net`, which
builds one explicitly).

Savings account, one month, a €500 checking→savings transfer and a €200
savings→checking transfer. Both legs sit on the savings account and **both carry
the same Transfer category** — `create_transfer` assigns one `category_id` to
both legs (`transaction_service.py:2354-2374`, auto-creating the `slug="transfer"`
system category if absent).

- `account_id=savings` + `category_id=Transfer` + `sum_amount` → **€700**
- with transfer-as-a-`txn_type`-value → **€700**

The defensible answers are €500 (gross in) or €300 (net). **€700 is neither,
and it renders with no warning.** Any design that cannot express "transfer leg
AND inbound" ships this number.

---

## Q4 — Cash basis and multi-currency

**Ruling: publishing `currency` on the transactions source is a PRECONDITION of
shipping any transfer/savings answer, not follow-up polish.**

The transactions source publishes no `currency` dimension and no `currency`
filter, and `_measure_expr` sums bare `Transaction.amount`. So on a
multi-currency org, today's transactions totals already add EUR to USD and print
one number — while `SourceMeasure(..., "currency")` labels that number as money.

The repo's own written rule, `reports/sources/networth.py:19-21`:

> NEVER sum across currencies (no FX). Always partition by currency; a
> multi-currency org with no `currency` dimension gets per-currency series +
> a `meta.warning`.

NetWorth honours it (`networth.py:269-292`). `BalancesByTypeTile` honours it
(`CurrencySubtotal`, `MAX_CURRENCY_LINES = 2`). **The transactions source is the
lone outlier**, and this ticket is what makes that outlier load-bearing for a
number the operator will read as authoritative.

⚠ This is also why "repairing Type=Transfer has zero regression surface" is
**false**. It is true that every persisted widget carrying that value renders
empty today — which is the point: the repair turns a visibly-empty tile into a
**populated, confidently wrong, cross-currency sum**. Empty → wrong is a worse
trade than empty → right, and nothing looks broken.

Minimum acceptable: publish `currency` as a transactions dimension + filter
(no enum widening, no migration; reuse the lazy join at
`reports_query_service.py:295-303`). Floor, if even that is deferred: the
`QueryMeta.warning` NetWorth already ships. Shipping with **neither** is not
acceptable.

**One invariant worth keeping:** every reciprocal transfer pair is
single-currency by construction — `_link_pair` rejects mismatched legs
(`transaction_service.py:1783-1794`, "Transfer legs must have the same
currency"). So a row can never straddle currencies *within* a pair; the exposure
is only the SUM *across* pairs, which partitioning fixes.

**Fence, and its vacuity trap:** two savings accounts, one EUR one USD, equal
amounts, one transfer each; assert two rows keyed by **currency**. ⚠ With two
accounts, a bug that partitions by *account* also yields two rows — the fence
must assert on the currency key *and* that no returned value equals the
cross-currency sum.

---

## Q5 — "Savings" already means five things

| # | Surface | What it actually answers | Verdict |
|---|---|---|---|
| 1 | Sankey `__hub_savings__` = `income − expense` (`sankey_service.py:58,194-197`), labelled "Savings" (`lib/reports/sankey-labels.ts:15`) | "how much income did I not spend?" | mislabelled — **out of scope, see below** |
| 2 | `dash_balances_by_type` "Savings" row (`BalancesByTypeTile.tsx:39-64`) | "where does the money sit *now*?" | **correct as built** |
| 3 | `financial_goals` category tree (`models/category.py:140-149`) | "what did I *call* this outflow?" | correct, orthogonal |
| 4 | "Savings Rate" KPI promised at `frontend/lib/help/tooltips.ts:130` | nothing — **it does not exist** | **false promise, fix it** |
| 5 | proposed transfer-flow measure | "how much did I move in?" | this ticket |

⚠ Correction to an earlier claim: there is **no category named "Savings"**. The
slug is `financial_goals` / "Financial Goals", `type="expense"`, description
"Savings and investment contributions", children `emergency_fund` /
`retirement` / `general_savings` / `investments`. An implementer who greps
categories for `"savings"` finds nothing.

**A user can already place #1 and #2 on one canvas and get two different
"Savings" numbers.** `WidgetType.SANKEY` and `WidgetType.KPI` are peers on the
same layout (`schemas/report_layout.py:64,197,237`) under shared canvas filters.
And they are not close: the Sankey residual counts a €500 checking→savings
transfer as **zero** (transfer legs are excluded by
`reportable_transaction_filter()`), while a flow measure counts it as **€500**.
A user paying rent *out of* savings gets a positive residual and a negative flow
in the same period.

**Rulings:**

1. **Do not unify.** They answer different questions and three of the five are
   correct within their own frame.
2. **The new measure names its subject: "Transferred to &lt;account type&gt;",
   never "Savings".** A label that does not name its subject reads as a
   contradiction the moment a second one appears
   (`reference_pending_email_ui_badge_ambiguity`).
3. **Fix #4 in this ticket** — one line, zero risk. It is a false promise shown
   to the user *in the exact surface where this complaint was formed*, and is
   plausibly part of why the operator concluded the data was untrackable. Same
   class as TBD-343's published-false-claims.
4. **#1's rename is explicitly OUT of scope.** Renaming a shipped, design-reviewed
   label is a design decision and goes through the visual-validation gate with the
   operator, not into a spike. File it.
5. **Never add a preset KPI called "Savings".** Ship the capability, let the
   operator build the widget and see what they name it.

⚠ No fence is recommended on a label rename. A string assertion on a label file
is a low-value ceiling a reviewer deletes in six months; the durable protection
is this document. Said plainly rather than manufacturing a green.

---

## Q6 — Double-counting

### ⚠ A live defect, outside this ticket's scope, blocking both routes

`transaction_filters.py:35` — `_RECON_EXCLUDED_STATES = ("skipped", "rejected")`.
`non_reverted_transaction_filter()` (`:68`) excludes only those two. But
`matched` is a distinct state (`models/transaction.py:122-131`) whose balance
contribution **has already been reverted** — `reconciliation_service.py:517-519`
is authoritative:

> A one-way matched row answers **False**: the MATCHED transition already
> reverted it.

That function's own docstring (`:69-80`) states the invariant it violates:
rows must be dropped "otherwise their amount double-counts against a balance
that no longer contains them."

Masked on the default path, because `reportable_transaction_filter()` drops
matched rows via `linked_transaction_id IS NULL`. Reachable **only** through the
shipped "Include transfers & adjustments" opt-in
(`FilterEditor.tsx:214-240`).

Reproduction: import a statement, reconcile-match a duplicate against the
canonical row, tick the toggle. The charge is counted twice.

**This lands first, as its own ticket.** Fence: an ACCEPTED canonical row plus
its MATCHED duplicate; assert the charge appears once. ⚠ A fixture without a
matched pair is green under the broken tuple — the same miss shape as F30/F31 in
`tests/services/test_matched_row_actions.py`.

### The canvas collision

A spend widget with transfers included, beside a transfer-flow widget: the
**same €500** appears as spending (the checking-side EXPENSE leg) and as savings
(the savings-side INCOME leg). Neither widget is wrong; together they are.
Aggravated by credit-card payments, which are transfers
(`org_bootstrap_service.py:47-56`) — so with transfers included, the payment
counts as spend on top of the purchases it settles.

Mitigation, not a fix: `lib/reports/describe-filters.ts` already renders the mode
into the widget subtitle. Make the transfer modes render **distinctly and
unmissably**. A user who can see the mode on each tile can reason; one who sees
two unlabelled figures cannot.

### The sign trap

Covered in Q3. A transfer-flow widget without a direction term reports **gross
traffic**. ⚠ Fence vacuity: a deposit-only fixture is green under both the broken
and the correct implementation. The fixture needs a deposit, a withdrawal, and a
savings→savings transfer (which puts **both** legs inside an account-type
filter).

---

## Scope ruling

**Build the operator's first sentence. Cut the second.**

Worth building: "how much did I move to savings this period" is answerable from
existing data, needs no migration, no new column, no new dataset and no enum
widening.

Not worth building now: round-up attribution (Q2).

## Ordered implementation plan

Each step is independently correct and independently shippable.

1. **Fix the `matched` leak** in `non_reverted_transaction_filter()`. Prerequisite
   for everything below and correct on its own merits. *Separate ticket.*
2. **Publish `currency`** as a transactions dimension + filter (Q4). Precondition.
   ⚠ Changes the catalog, so `frontend/tests/fixtures/report-sources.json` must be
   regenerated and **the diff read** — `test_report_sources_frontend_contract.py`
   going red is the designed signal, not a cost.
3. **Add a fail-closed reciprocal-transfer clause** to `transaction_filters.py`,
   beside its two siblings, documenting its polarity against them. ⚠ It fails
   **CLOSED**; `balance_contribution_filter()` deliberately fails **OPEN**
   (`:118-136`) and is frozen under TBD-280 — do not touch it.
4. **Put transfer-ness on its own axis**, composable with `txn_type` (Q3), and
   **repair or retire the dead `Type = Transfer` value** so the control stops
   lying. Publish `account_type` as a transactions dimension + filter in the same
   slice (Q1).
5. **Delete the "Savings Rate" promise** at `frontend/lib/help/tooltips.ts:130`.

Deferred deliberately: any named "Savings" KPI or template (Q5 ruling 5).

## The wrong implementations this spike rules out

| Wrong implementation | Why it is wrong | Mutant a fence must kill |
|---|---|---|
| `WHERE AccountType.slug == "savings"` | user-created types have `slug IS NULL` → silent `0.00` | user-created type, non-zero expected |
| `linked_transaction_id IS NOT NULL` as "is a transfer" | `_apply_match` writes it **one-way** for reconcile matches | one-way matched row + real pair, identical otherwise; assert only the pair counts |
| transfer as a fourth `txn_type` value | OR-multiselect; "transfer AND inbound" inexpressible → €700 | in-and-out fixture; assert net or gross-in, never the sum of both |
| a new `Dataset.SAVINGS_FLOW` | trips the only exact-set enum fence; security review; buys nothing | — |
| an `intent`/`provenance` column now | 100% NULL, zero writers | — |
| shipping any of this without currency partitioning | silent EUR+USD sum labelled as money | two-currency fixture keyed on currency |

## Tickets to file

- The `matched` leak (step 1) — defect, prerequisite.
- Round-up engine + attribution (Q2) — product feature, not reporting.
- Sankey "Savings" → "Unspent" rename (Q5 ruling 4) — needs the visual gate.
- Implementation ticket for steps 2-5.

## Related

Epic TBD-380. `reference_linked_transaction_id_three_writers` ·
`reference_code_property_used_as_data_property` ·
`reference_fence_records_coverage_not_path` ·
`reference_pending_email_ui_badge_ambiguity`.
