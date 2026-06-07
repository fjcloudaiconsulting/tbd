# Product

## Register

product

## Users

Individuals and households who plan their financial lives, not just track them. The seed user is a finance-savvy operator who currently runs a manual monthly spreadsheet (per-line-item budget + forecast across multiple accounts and a credit card). The product expands to couples and families managing shared finances through the org-scoped data model (every account, transaction, budget, and forecast belongs to an organization, not a single user).

The job to be done is **monthly financial planning**, not daily transaction logging. Users want to see, on any given day, what they planned to spend, what they have spent, and whether the rest of the month still works. They open the app to make decisions, not to data-enter.

## Product Purpose

The Better Decision replaces the user's manual spreadsheet workflow (income, expenses, status per line, executed vs forecast totals, per-account separation) with an app that automates the repetitive parts (recurring transactions, monthly rollover, status tracking, import from bank CSVs) while preserving the level of visibility a spreadsheet gives. The core mental model is **Budget vs Forecast vs Actual** at line-item granularity, not just category totals.

Success looks like: a household opens the app on the 15th of the month and within ten seconds knows whether they're on track, what's pending, and what their forecast end-of-month balance is, without scrolling through transactions.

## Brand Personality

Clear, household-friendly, planful. Three notes:

- **Clear** before stylish. Numbers, dates, statuses, and balances are the heroes; chrome serves them.
- **Household-friendly** means warm enough for shared use without being toy-cute. No paternalistic "Dear Customer" tone, no playful illustrations, no condescension. Plain, direct language a partner can read over a coffee.
- **Planful** means the app rewards thinking ahead. Forecasts are as visible as actuals. The Budget vs Forecast tile is core, not a buried report.

## Anti-references

- **Bank apps.** Heavy navy-and-white corporate chrome, formal copy, paternalistic tone, big circular avatar greeting. The Better Decision is not a bank — it is a planning tool that happens to handle money.
- **Spreadsheet skins.** The app replaces a spreadsheet but cannot look like one. Hierarchy must come from typography, color, spacing, and grouping — not gridlines and uniform rows. If a screen reads like Google Sheets in a wrapper, it has failed.

## Design Principles

1. **Plan-first, not transaction-first.** Forecasts, budgets, and recurring plans are as prominent as actuals. The home view answers "where am I vs where I planned to be," not "list me every transaction."
2. **Line-item visibility, not just totals.** The user came from per-line budgets in a spreadsheet. Aggregates without drill-down to the underlying lines violate the contract. Every total has a path to its constituent rows.
3. **Hierarchy without grids.** Tables exist where they earn their place, but visual hierarchy comes from weight, scale, color, and grouping. Gridlines are the last resort, not the default.
4. **Quiet by default, expressive when it matters.** The gold accent (`#D4A64A`) is reserved for primary CTAs, positive balances, and the active row in a list. It is not chrome decoration. If gold appears in three places on a screen, two of them are wrong.
5. **Status is data.** Pago/Aberto, settled/pending, planned/actual — these distinctions are first-class. The app should never collapse them into a single number when the difference is the whole point.

## Accessibility & Inclusion

WCAG 2.2 AA across the app. Specifically:

- Color contrast meets AA against both the dark default theme and the light theme.
- Focus states are visible on every interactive element (the gold accent doubles as the focus ring color).
- `prefers-reduced-motion` is respected for any non-essential motion (page transitions, chart animations).
- Data visualizations never rely on color alone to convey state; budget bars, forecast tiles, and status badges always pair color with a label, icon, or shape.
- Currency, dates, and decimals are formatted per the user's locale (multi-currency and i18n are post-launch goals, but the design assumptions must not block them).
