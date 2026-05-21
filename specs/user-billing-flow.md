---
name: User Billing Flow
description: fjorge's real-world billing/cycle workflow — informs the org-admin period UI redesign brainstorm
type: project
originSessionId: 1d48e329-4340-4f5d-a67c-182b3d3f7479
---
How fjorge actually runs his finances (target UX context for the period management redesign):

- **Period anchor = salary day.** The new period starts the day salary lands; the previous period closes the day before. Salary day is known but varies (~23rd–25th).
- **Forecast spillover.** Some forecasted income/expenses don't settle on the planned day. When that happens, the entry is pushed to the new period and the new period's forecast is bumped to absorb the unexpected slip.
- **Provider charge dates are not fixed**, which is why spillover happens.
- **Multiple credit-card cycles.** Card A closes ~25th, card B closes ~5th. After salary lands (e.g., the 25th), card B charges between then and the 5th are added to the previous, already-closed period — because the user treats "the period that owns this card cycle" as the close target, not the calendar period.

**Why:** This is the workflow fjorge wants the app to mirror; it's why the simple "one open period at a time" model is too rigid.

**How to apply:** When designing the period roster UI (12-month list with inline close/edit, December-rolls-to-next-Jan), the design must support: (1) reopening or editing the immediately previous closed period, (2) moving transactions across the close boundary after the fact, (3) adjusting forecast on the new period at close time. Don't assume calendar-month alignment.

**Per-period start AND end dates must both be user-editable.** Today the only knob is org-level `billing_cycle_day`, and `ensure_future_periods` derives both start and end from it (start = cycle day each month, end = day-before-next-start). User wants to set, e.g., a period "Apr 25 → May 24" explicitly per row. The `BillingPeriod` model already has `(start_date, end_date)` as separate nullable columns, so the schema is fine — it's a UI surface gap. Cycle day can remain a seed default for new stubs but should not constrain per-row edits.
