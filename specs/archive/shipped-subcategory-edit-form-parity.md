---
name: Sub-category edit form — missing description field — SHIPPED #324
description: SHIPPED via PR #324 on 2026-05-20. Top-level + sub-category edit forms now expose description input. Backend gained Pydantic v2 model_fields_set check at categories.py:411 so explicit-null clears (was conflated with "field omitted" before).
type: project
originSessionId: 31bd894a-67ce-4301-b8b1-880672646504
---
**Status:** SHIPPED via PR #324, merged 2026-05-20. Two commits: initial fix + review-feedback follow-up (`efa8c50`).

## What landed
- `frontend/app/categories/page.tsx` — `editCatDesc` state + description `<input>` in both the master and sub-category inline edit forms (one shared handler, two render sites). PUT body includes `description` (trimmed; empty → `null`).
- `backend/app/routers/categories.py:411` — `if "description" in body.model_fields_set:` (was `if body.description is not None:`). Pydantic v2 PATCH semantics: explicit null now clears the column, field omission still preserves the existing value.
- `CategoryUpdate.description: Optional[str] = None` schema was already correct — no schema change required.
- New tests:
  - `backend/tests/routers/test_categories_update_description.py` — 3 cases (explicit-null clears, omitted preserves, new value persists).
  - `frontend/tests/app/categories-c2-edit-mode.test.tsx` — 3 new cases (sub-edit, top-level parity, empty-desc → null with after-save UI reconciliation).

## Why the review-cycle was needed
The first PR pass only asserted the outbound PUT body. It didn't catch the silent-no-op on the backend (`is not None` dropping explicit nulls). Architect caught it; second commit added `model_fields_set` + the persistence test that exercises the full clear-then-reload reconciliation path.

## Lesson for future Optional-field fixes
"Test the outbound request body" ≠ "test persistence." Always exercise the after-save reconciliation path on partial updates. The pattern locks into [[reference_ci_driver_contract_testing]] family of "contract verified against the OTHER side, not against our own assumption."

## Why this memory is kept
To prevent re-picking the shipped task off the roadmap. The roadmap entry [Sub-category edit form missing description] in `MEMORY.md` needs to move to archive — done by the same session that finds this memory.
