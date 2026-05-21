---
name: Billing Cycle and Transaction Status Design
description: Design decisions for billing cycles, transaction status, recurring transactions, and dashboard improvements
type: project
---

## Billing Cycle
- Org-level setting for month close date (e.g., 15th instead of 1st)
- All monthly views (dashboard, forecasts) respect this cycle
- Per-account reporting is a future nice-to-have but not the primary view

## Transaction Status
- Two states: `settled` and `pending`
- Settled transactions affect actual account balance
- Pending transactions only count toward monthly forecast
- Status is always editable by the user

## Recurring Transactions
- Templates with frequency (weekly, monthly, etc.)
- Auto-generate `pending` transactions ahead of time
- Can be settled manually by the user or auto-settled on the due date
- Editable — user can change status, amount, date after generation

## Dashboard Improvements
- Quick-add transaction form on dashboard
- Last 10 transactions with color-coded amounts
- Pagination scoped to current billing cycle (month)
- Link to full transactions page for broader views

## Transactions Page Filters
- By type (income/expense)
- By status (settled/pending)
- By account
- By category
- By date range

**Why:** User manages credit cards with different settlement dates. All bills paid from same income, so org-level cycle makes sense. Pending transactions give visibility into upcoming obligations without corrupting actual balances.

**How to apply:** Build in phases — status + dashboard first, then recurring, then billing cycle. All within the transactions domain while we're still building it out.
