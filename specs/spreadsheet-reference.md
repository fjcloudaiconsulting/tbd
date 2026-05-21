---
name: Spreadsheet Reference
description: User's current spreadsheet workflow — the target experience to replicate and improve upon
type: project
---

## User's Current Workflow (Google Sheets)

The user manages finances in a monthly spreadsheet with the following structure:

### Left side: ING Expenses
Each line item has: Description, Executed amount, Status (Pago/Aberto), Budget Balance, Budget, Budget Forecast.
Examples: Rent (2,691.15), Car payment (695.59), Health plan (16.66), Groceries (1,200), etc.

### Right side: ING Income
Each line: Description, Executed, Status (Pago), Forecast.
Examples: Salary (10,606.61), Karaoke income (93.60), Government aid (770.36), etc.

### Right side: Credit Card (Amex) Expenses
Separate section for CC: Executed vs Forecast per line.
Examples: iCloud, Spotify, Disney+, Netflix, Supplements (-169.54), AWS (-18.18), LinkedIn (-39.99), Viagem Baltics (-821.79), etc.

### Summary (far right)
- Executed: Income 29,021.86 / Expenses -19,090.82 / Balance 9,931.04
- Forecast: Income 29,021.86 / Expenses -19,090.82 / Balance 9,931.04

### Account Totals (bottom)
- Amex: -3,178.73
- ING: -1,227.75
- Total: -4,406.48

### Savings
- Balance: 9,931.04 (shown separately)
- Savings: 6,081.46

## Key Features to Replicate

1. **Line-item budget + forecast** — not just category totals, each expense line has its own budget and forecast
2. **Executed vs Forecast dual view** — see what happened vs what was planned
3. **Per-account expense separation** — ING expenses vs Credit Card expenses as distinct sections
4. **Status per line** — Pago (settled) vs Aberto (pending) — we have this
5. **Budget balance per line** — remaining from allocated budget per expense
6. **Summary: executed AND forecast** — the total picture for the month
7. **Manual monthly process** — user copies sheet each month and adjusts, wants automation

## What PFV2 Already Covers
- Status (settled/pending) ✓
- Per-account filtering ✓
- Category-level budgets ✓
- Billing periods ✓
- Recurring transactions ✓

## What's Missing (Phase 3b/3c targets)
- Line-item forecasting (pending + recurring = forecast)
- Executed vs Forecast comparison view
- Per-account expense grouping view
- Summary tiles: executed totals + forecast totals side by side
- Savings calculation (income - expenses = available for savings)

**Why:** This spreadsheet is the user's actual workflow. The app needs to match this level of financial visibility while automating the manual parts (monthly copying, status tracking, recurring items).
