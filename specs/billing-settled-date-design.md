---
name: Billing Period Settlement Design
description: settled_date field determines which billing period a transaction counts against — critical for CC late settlements
type: project
originSessionId: abad3c60-c398-4e0e-851e-811d6900a085
---
**Design decision (2026-04-13):** Transactions have both `date` (purchase date) and `settled_date` (when settled).

**Why:** CC transactions that settle after a billing period closes should count against the NEXT period, not the period they were purchased in. This matches real-world CC billing behavior.

**How to apply:**
- `Budget spend` queries filter by `settled_date` (not `date`)
- `Forecast executed` queries filter by `settled_date`
- `Forecast pending` queries still use `date` (for visibility in the period the purchase happened)
- `settled_date` is set to `date` when created as settled, `today()` when toggling pending→settled, cleared when toggling settled→pending
- ALL Transaction() creation paths must set `settled_date` when status is SETTLED (transaction_service, recurring_service)
- Index: `ix_transactions_org_settled_date` on (org_id, status, settled_date)
