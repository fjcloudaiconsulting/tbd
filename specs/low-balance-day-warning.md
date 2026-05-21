---
name: Low-Balance-Day Warning (cashflow risk forecast)
description: Surface a warning when the user's expected balance on a specific day of the month dips below upcoming expenses, based on recurring + pending + historical patterns.
type: project
---
**Captured 2026-05-10.** Not a launch blocker. Rough idea, sized M-L, parks for future prioritization.

## Concept

Warn the user when an upcoming day in the current (or near-future) period is projected to leave them unable to cover expenses falling on that day. Surface as either:
- A new Dashboard card ("Cashflow risk: 2026-05-22"), OR
- A signal on the Accounts card / Forecast section, OR
- An inline highlight on the day in a calendar/timeline view (post-launch).

Final placement TBD; test in user research.

## Inputs

- **Pending transactions** with `settled_date` (PR #157 + #163 work). Already a first-class field.
- **Recurring transactions** generation (templates that materialize on `next_due_date`).
- **Historical patterns** (optional v2): if the user typically pays X on day 25 even without a recurring template, flag it.
- **Current account balance(s)** at compute time.
- **Per-account or aggregate** — open question (probably aggregate per currency to start).

## Algorithm sketch

```
For each day D between today and end-of-period:
    expected_balance(D) = current_balance
                        + sum(pending_inflows where settled_date <= D)
                        - sum(pending_outflows where settled_date <= D)
                        + sum(recurring_inflows where next_due_date <= D and within period)
                        - sum(recurring_outflows where next_due_date <= D and within period)
    if expected_balance(D) < 0 OR expected_balance(D) < threshold:
        flag D as risk day, with the obligations falling on/by D
```

Threshold can be 0 (overdraft) or a user-set buffer (e.g., "warn if I drop below 200 EUR").

## Open questions

1. **Per-account vs aggregate.** Per-account is more accurate (a credit card doesn't bail out a checking account at the bank level). Aggregate is simpler. Probably do per-account once the data model supports it cleanly.
2. **Currencies.** Multi-currency is post-launch (P3); v1 should be single-currency-aware.
3. **Historical-pattern detection.** Looks like a small ML or rule-based pass over `transactions` grouped by description/merchant. Out of scope for v1; add to LAI tier if useful.
4. **UX.** Card vs banner vs calendar overlay vs inline-on-Forecast. Test before committing.
5. **False-positive cost.** Warning the user about a "risk day" when they have a settlement coming creates anxiety. Rules must be conservative.

## Cross-references

- `project_billing_settled_date.md` (settled_date semantics)
- `2026-05-08-forecasts-ux-restructure.md` (per-account month-end balance — same data shape, different time slice)
- Punch list item 13 (settled_date in form, shipped via PR #197)
