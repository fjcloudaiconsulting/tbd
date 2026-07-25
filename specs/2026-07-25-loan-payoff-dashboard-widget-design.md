---
name: Loan Payoff Dashboard Widget + Shared Loan-Status Extraction
description: Group C item 7. An opt-in dashboard tile (dash_loan_payoff) surfacing per-loan payoff status + next payment, plus extraction of a shared loanPayoffStatus() classifier so the accounts card and the tile can't drift.
type: project
status: design — APPROVED-WITH-CHANGES (both sign-offs folded); ready to build
---

# Loan Payoff Dashboard Widget (`dash_loan_payoff`) + shared loan-status unit

**Roadmap:** Group C, item 7. **Effort:** M. **Method:** architect-gated (2 design reviews + 2 spec sign-offs, all folded → TDD build → code review → PR). Autonomy grant 2026-07-25: operator is the merge gate only.

## 1. Goal & guardrails

Loans have **zero** dashboard presence today; credit cards have `dash_cc_utilization`. This closes that asymmetry with a glanceable, **opt-in** tile.

Non-negotiable guardrails (roadmap):
- **Glanceable SUMMARY, not a transplant** of the full `LiabilityCards` detail card. Payoff status + next payment only.
- **Must NOT restate `dash_balances_by_type`** — earns its place via **payoff status + next payment**, never balance. Balance is deliberately absent.
- **Opt-in**, not in the default seeded layout (same posture as `dash_cc_utilization` / `dash_balances_by_type`).

## 2. Decisions (settled; both sign-offs folded)

| # | Decision | Choice | Rationale |
|---|---|---|---|
| D1 | Data source | Reuse `DashboardDataProvider` (`activeAccounts` + `accountMonthEndForecast`); **no new endpoint** | All needed data is in-context; matches the `config: {}` invariant. Verified: both exposed by `useDashboard()`. |
| D2 | Shared unit | Extract `loanPayoffStatus()` into new `frontend/lib/loan.ts`; returns **`{state, tone}`** (pure classification, NO styles import — mirrors `lib/credit.ts` exactly) | The drift-prone invariant is `state→tone`. `tone→className` is trivial presentational mapping that legitimately varies per surface, so it stays OUT of `lib/loan.ts`. (Sign-off A I1 + B D8.) |
| D3 | Labels | **Per-surface**, NOT shared. Accounts card keeps verbose copy; tile uses short labels | Sharing verbose labels would drag the "transplant" into the glanceable tile. |
| D4 | CC scope | Loan only this PR; touch no CC code | CC utilization math already lives in `lib/credit.ts` and backs both surfaces. No CC status-chip exists; a parallel would be a no-second-consumer abstraction. |
| D5 | Sort order (N loans) | Attention-first: `interest_only` → `setup` (null metrics) → `on_track` → `paid_off`; tie-break soonest next-payment date, then name | Surfaces action items (not paying down principal; incomplete data) above reassurance (on_track) above done (paid_off). Deliberately NOT balance-sorted. (Sign-off B minor folded: setup floated above on_track.) |
| D6 | Icon + title | lucide `Landmark`, title "Loan payoff" | `Landmark` is already the loan icon in `BalancesByTypeTile`. |
| D7 | Grid footprint | `w: 4, h: 6` (h=6 = `MIN_CONTENT_H` floor); nominal `y: 37` | Same footprint class as `dash_cc_utilization`; opt-in so `y` is nominal but must match across sites #2 and #6. |
| D8 | tone→badge map home | Generic `badgeForTone(tone)` in **`lib/styles.ts`** (tone = info/success/neutral/warning is generic, maps 1:1 to existing `badge*` tokens) | Single shared map → card and tile can't drift; keeps `lib/loan.ts` pure; `lib/styles.ts` is the right home for token resolution. |

## 3. Data model (grounded; verified by sign-off A)

- **Payoff facts** on `activeAccounts[].loan: LoanMetrics | null` (`frontend/lib/types.ts:149-156`), server-computed by `loan_service.compute_loan_metrics`, projected via `AccountResponse.loan`. Fields: `expected_monthly_payment`, `maturation_date`, `total_interest`, `projected_payoff_date` (null for `interest_only`), `projected_payoff_months`, `status ∈ {on_track, paid_off, interest_only}`. `loan` is **null** when the account is a loan slug but not fully specified (finish-setup).
- **Next payment** (date + amount) is NOT on `LoanMetrics`. It comes from `accountMonthEndForecast.accounts[].loan_payments[0] = {amount, date}` (backend `LoanPaymentLine` `schemas/forecast.py:33-52`; frontend `AccountMonthEndForecastRow.loan_payments?: {amount,date}[]` in `AccountMonthEndForecast.tsx:27`) — the analogue of `cc_payments[0]` that `CreditUtilizationWidget` consumes (`:50-57,83`). **Join:** build a map keyed by the forecast row's `account_id`, look it up by the frontend `Account.id` (`types.ts:88`).
- `activeAccounts` is already `is_active`-filtered by the provider — no inactive handling needed.
- `accountMonthEndForecast` is fetched only for the current period; if it's null (fetch error/loading) the tile degrades gracefully (§10 edge 5).

## 4. Shared extraction — `frontend/lib/loan.ts` (new) + `badgeForTone` in `lib/styles.ts`

`lib/loan.ts` (pure; imports only `LoanMetrics` type — mirrors `lib/credit.ts`, no styles import):

```ts
import type { LoanMetrics } from "@/lib/types";
import type { BadgeTone } from "@/lib/styles";

export type LoanPayoffState = "setup" | "on_track" | "paid_off" | "interest_only";

export interface LoanPayoffStatus {
  state: LoanPayoffState;   // null/undefined loan normalizes to "setup"
  tone: BadgeTone;          // "info" | "success" | "neutral" | "warning"
}

export function loanPayoffStatus(loan?: LoanMetrics | null): LoanPayoffStatus;
```

State→tone (locks today's accounts render): `setup→info`, `on_track→success`, `paid_off→neutral`, `interest_only→warning`. `!loan ? "setup" : loan.status` type-checks to `LoanPayoffState`.

`lib/styles.ts` gains a generic reverse map (tones already correspond 1:1 to the four `badge*` tokens):

```ts
export type BadgeTone = "info" | "success" | "neutral" | "warning";
export function badgeForTone(tone: BadgeTone): string; // info→badgeInfo, success→badgeSuccess, neutral→badgeNeutral, warning→badgeWarning
```

### 4.1 Render-preserving `LoanCard` rewire (`LiabilityCards.tsx`, branch `accounts-liability-zone-label`)

Rendered output MUST NOT change (existing component test is the proof).
- Keep the early `if (!m)` guard (preserves TS narrowing for the on_track branch reading `m.projected_payoff_date`).
- Resolve the chip class via `badgeForTone(loanPayoffStatus(m).tone)` instead of inline `badgeSuccess`/etc.
- Keep every label string **byte-for-byte** ("Finish setting up this loan", "Paid off", "Payment covers interest only", "On track · paid off by …" / "On track").
- Imports (line 23): add `import { loanPayoffStatus } from "@/lib/loan";`; replace the four `badge*` tokens with `badgeForTone` from `@/lib/styles`; **keep `cardTitle`** (#579 added it, heading still uses it).
- No change to `CardShell`, `CreditCardCard`, `Metric`, `PaidFrom`, sort/heading logic, testids, or the `<section>`/`<h2>` structure. tone→badge resolves to the identical token per status, so render is byte-for-byte preserved.

## 5. Widget content & layout (`LoanPayoffTile`)

Consumes `loanPayoffStatus(account.loan)` for `{state, tone}` and supplies its **own short labels**:

| state | tile label | tone |
|---|---|---|
| on_track | "On track" | success |
| interest_only | "Interest only" | warning |
| paid_off | "Paid off" | neutral |
| setup (null) | "Needs setup" | info |

- **Status = the single expressive (colored) moment**, rendered as ONE badge per row via `badgeForTone(tone)`.
- **Next payment = plain muted inline text (NOT a chip):** `text-xs text-text-muted` reading `"Next payment {formatAmount(amount)} {currency} on {date}"` (word "on", matching `CreditUtilizationWidget:94`), rendered only when `loan_payments[0]` exists (paid-off / interest-only typically have none; simply omitted). Demoting this from a badge keeps the tile calm across N rows (sign-off B priority). Its distinct wording vs the card's "Monthly payment" is load-bearing (forecast next-payment ≠ nominal monthly for a final/partial payment).
- **Payoff-by date intentionally omitted** from the tile (lives in the accounts card). "On track" + next-payment date is the glanceable summary.
- **No balance, no rate/term/matures/interest** (those are the detail card — a negative test locks this, §8).

Layout per count:
- **0 loans** → empty state: centered `text-text-muted` "No loans yet." + `text-text-primary underline` "Add one" → `/accounts` (byte-consistent with both sibling tiles' empty states).
- **1 loan** → single row: account name (`text-sm font-medium`) + status badge + muted next-payment line.
- **N loans** → vertical list, `divide-y divide-border-subtle` between rows (from `BalancesByTypeTile`), sorted per D5. Row region may scroll if tall.
- **null-metrics row** → a real row with "Needs setup" (info) badge, no next-payment/metrics. Never dereference `m.*` when null.

Chrome (byte-consistent with siblings): `${card} flex flex-col overflow-hidden`; header `flex items-center justify-between ${cardHeader}`; **`<h2 className={cardTitle}>Loan payoff</h2>`**; right-aligned quiet "Accounts" `Link` to `/accounts` copying the sibling class `text-xs text-text-secondary underline underline-offset-2 hover:text-text-primary` **plus an explicit brass focus ring** (see §7). The "Add one" empty-state link carries the same brass focus ring.

## 6. The 7-site registration checklist (+ 8th test touch; mirror `dash_cc_utilization`)

ids: widget `dash_loan_payoff` · component `LoanPayoffTile` · enum `LOAN_PAYOFF` · class `DashLoanPayoffWidget`.

| # | Site | Change |
|---|---|---|
| 1 | `frontend/components/dashboard/widgets/LoanPayoffTile.tsx` (NEW) | The component. `useDashboard()`, `activeAccounts.filter(a => a.account_type_slug === "loan")`, join `loan_payments` (map by forecast `account_id`, lookup by `Account.id`), render status badge + muted next-payment line. |
| 2 | `frontend/lib/dashboard/widget-types.ts` | Add `"dash_loan_payoff"` to `DashboardWidgetType` union **and** a `DASHBOARD_WIDGET_DEFAULTS` entry `{ title:"Loan payoff", grid:{x:0,y:37,w:4,h:6} }`. |
| 3 | `frontend/components/dashboard/renderDashboardWidget.tsx` | Import `LoanPayoffTile` + `case "dash_loan_payoff": return fill(<LoanPayoffTile/>);`. **⚠ Miss = silent blank** (default arm delegates to `renderReportWidget` → null). Covered by the new site #8 test. |
| 4 | `frontend/components/dashboard/AddWidgetMenu.tsx` | Add `Landmark` to the lucide import block (currently absent) **and** a `DASH_TILES` entry `{ type:"dash_loan_payoff", label:"Loan payoff", description:"Payoff status and next payment for each loan.", Icon: Landmark }` (no em-dash). |
| 5 | `backend/app/schemas/dashboard.py` | `LOAN_PAYOFF = "dash_loan_payoff"` in `DashWidgetType`; `class DashLoanPayoffWidget(_DashWidgetBase)` with `type: Literal[...]` + default `_DashWidgetConfig`; add to the `_DashboardWidget` Union. |
| 6 | `frontend/tests/lib/dashboard/widget-defaults.test.ts` | Add `dash_loan_payoff` to **both** `CANONICAL_GRIDS` (== site #2 grid) and `MIN_CONTENT_H` (`6`). |
| 7 | `backend/tests/schemas/test_dashboard_schemas.py` | Validation test: `dash_loan_payoff` validates + `config == {}`. |
| **8** | `frontend/tests/components/dashboard/dashboard-widget-registry.test.tsx` | **NEW render-dispatch assertion**: `renderDashboardWidget` for a `dash_loan_payoff` widget renders the tile's root testid. This is the ONLY test that protects site #3 (sign-off A B1: the existing "registry walk" only covers 3 legacy tiles, not dash_* tiles). |

**Do NOT** touch `DEFAULT_DASHBOARD_LAYOUT` in `backend/app/routers/dashboard.py` — keeps `test_default_layout_contains_seven_dash_tiles` (`len==7`) green (proves opt-in).

## 7. Brand / design compliance

- **Single expressive moment** = the one status badge per row. Next-payment is muted text, not a competing chip. Calm-at-rest holds across N rows (sign-off B fix).
- **One Brass Rule:** no gold/`accent` on the tile body; status uses semantic status tokens, never brass.
- **Pressable-Surfaces Rule:** the "Accounts" and "Add one" links MUST carry an explicit brass `focus-visible` (do NOT inherit `CreditUtilizationWidget`'s ring-less links, which violate the rule). Use the `BalancesByTypeTile` pattern: `focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent`.
- **Status-is-data:** four states are first-class distinct badges; every badge carries text so color is redundant, not load-bearing (WCAG).
- **AA note (not this PR's defect):** on the **dark** (default) theme all four badge tokens clear AA on the tile surface (≈5.2-7.4:1). On the **light** theme `badgeSuccess` (≈3.1:1) and `badgeInfo` (≈4.0:1) are marginal/failing — but these are **pre-existing systemic token values** already shipping across `LiabilityCards` and everywhere `badge*` is used; this tile introduces no new token. Tracked as a separate systemic token-contrast follow-up, out of scope here.

## 8. Tests

- **`frontend/tests/lib/loan.test.ts`** (NEW, mirror `credit.test.ts`): all four `status` values → expected `state`+`tone`; `null`+`undefined` → `setup`/`info`.
- **`frontend/tests/lib/styles` coverage** for `badgeForTone`: each tone → the matching `badge*` token (small, can live in the loan or a styles test).
- **`frontend/tests/components/accounts/liability-cards.test.tsx`** must stay green **unchanged** (render-preservation proof). Optional hardening: assert the paid_off/interest_only/on_track chip text to lock the label contract.
- **`frontend/tests/components/dashboard/loan-payoff-tile.test.tsx`** (NEW, mirror `credit-utilization-widget.test.tsx`): empty; single; N + sort order (D5); each status → correct badge tone/label; needs-setup row; next-payment present/absent; `accountMonthEndForecast==null` degradation (status badges still render, next-payment omitted); `projected_payoff_date==null` on on_track; **multi-currency** (two loans, different currencies, each renders its own `{amount} {currency}`, no summation); **guardrail-lock negative assertion**: no balance string and no rate/term/matures label appears in the tile.
- **`frontend/tests/components/dashboard/dashboard-widget-registry.test.tsx`** (site #8): render-dispatch for `dash_loan_payoff` renders the tile.
- **`widget-defaults.test.ts`** grid-sync; **`test_dashboard_schemas.py`** validation; existing `test_default_layout_contains_seven_dash_tiles` stays `len==7`.

## 9. Base-branch dependency (build order)

Build on **`accounts-liability-zone-label`** (PR #579), NOT `main`. The `LoanCard` import line (23) is edited by both #579 (added `cardTitle`) and this refactor (swaps the four `badge*` tokens for `badgeForTone`) — the conflict hotspot. If #579 merges first, rebase onto main; else branch from #579. Keep `cardTitle`.

## 10. Edge cases

1. Silent-blank render arm (site #3) — now covered by the site #8 render-dispatch test.
2. `loan == null` guard — never dereference `m.*`.
3. `projected_payoff_date == null` on on_track — defensive fallback (match `LoanCard`).
4. No `loan_payments` — omit the line, never "Next payment undefined".
5. `accountMonthEndForecast == null` (forecast error/loading) — status badges still render from `activeAccounts.loan`; next-payment degrades away silently.
6. Multi-currency — always `{amount} {account.currency}`; **never sum across loans** (no aggregation; introduce no `+`). Tested.

## 11. Architect sign-off

- Design review A (widget + registration): folded into §3, §5, §6, §7, §10.
- Design review B (extraction): folded into §4, §9.
- **Spec sign-off A (registration/data): APPROVE-WITH-CHANGES** — folded: B1 (site #8 render-dispatch test + removed the false registry-walk claim), I1 (D2 now returns `tone`, `lib/loan.ts` stays pure), M1 (`Landmark` import), M2 (`account_type_slug` + join keys named), M3 (guardrail-lock + multi-currency tests).
- **Spec sign-off B (design/UX/a11y): APPROVE-WITH-CHANGES** — folded: next-payment → muted text (calm-at-rest), D8 returns `tone` + `badgeForTone`, copy "on {date}", brass focus ring on links, softened §7 WCAG claim, "Needs setup" label, `setup` above `on_track` sort, pinned `<h2>`.
