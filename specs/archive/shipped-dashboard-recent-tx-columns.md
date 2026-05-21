---
name: Dashboard Recent Transactions Column Order + Status Column — SHIPPED #256
description: SHIPPED via PR #256 on 2026-05-13. Dashboard Recent Transactions now matches /transactions column order (Date → Description → Status → Amount) with sortable Status pill and right-aligned tabular-nums Amount.
type: project
---
**Status:** SHIPPED via PR #256, commit `44b8bf3`, merged 2026-05-13.

## What landed
- `frontend/app/dashboard/page.tsx` (~L1115-1232) renders Recent Transactions columns in canonical order: **Date → Description → Status → Amount** — a subset of `/transactions`' order (Date → Description → Account → Category → Status → Amount at `frontend/app/transactions/page.tsx` ~L1260-1265).
- Status is a sortable column using standardized pill tokens (`bg-success-dim text-success` for settled, `bg-warning-dim text-warning` for pending).
- Amount is right-aligned, tabular-nums, rightmost cell.
- Regression coverage at `frontend/tests/app/dashboard-recent-tx-columns.test.tsx` asserts the document order Date → Description → Status → Amount.

## Original concept (kept for context)
Dashboard's Recent Transactions list and the `/transactions` page had different column orderings, breaking consistency for users moving between the two. The fix brought Dashboard into alignment with `/transactions`. Captured 2026-05-10, sized XS-S, shipped 3 days later.

## Why this memory is kept
To prevent re-picking the shipped task off the roadmap. The roadmap entry [Dashboard Recent Tx Columns] in `MEMORY.md` under "Open backlog items" needs to move to archive — done by the same session that finds this memory.
