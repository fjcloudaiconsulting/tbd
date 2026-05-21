---
name: Flaky frontend test — transactions-page Task D7 transfer-wiring action column
description: Frontend test tests/app/transactions-page.test.tsx > "TransactionsPage — transfer wiring (Task D7) > Per-row action column renders all actions on a single un-linked row (responsive layout)" intermittently fails in CI on getByRole('button', { name: /Mark as transfer: Solo tx/i }). Backend-only PRs (no frontend diff) have hit it. Likely a timing/visibility race with the responsive-layout DOM tree.
type: project
originSessionId: 8c696f02-828f-45cb-8352-6fc04e4fb413
---
# Flaky test: transactions-page Task D7 action column

**First spotted:** 2026-05-12 during the Wave 2A/B parallel sweep. **Recurring on at least:** PR #244, #246, #248, #261 (all unrelated scopes; backend-only PRs trigger it too).

## Symptom

```
TestingLibraryElementError: Unable to find an accessible element
  with the role "button" and name /Mark as transfer: Solo tx/i
```

Test file/name: `frontend/tests/app/transactions-page.test.tsx > TransactionsPage — transfer wiring (Task D7) > Per-row action column renders all actions on a single un-linked row (responsive layout)`.

Always fails on the FIRST assertion that searches for the "Mark as transfer: Solo tx" button. Passes 4-5/5 times locally and on CI rerun.

## Why it's a flake (not a real regression)

- Backend-only PRs (zero frontend files modified) trigger the same failure.
- The test passes on rerun every observed time so far.
- The accessibility-tree dump in the failure shows the form-control surface but the button row is absent from the snapshot, suggesting a render-timing issue in the responsive-layout DOM that the test queries before it stabilizes.

## Likely root cause hypotheses

1. **Responsive layout double-render race.** The test runs in jsdom which doesn't honor CSS media queries the same way a real browser does. The component may render both the mobile + desktop branches and the action button is in the branch the test's query path doesn't reach on cold cache.
2. **Async state not awaited.** A `useEffect` in TransactionsPage may set state after first paint, and the test queries `getByRole` before the second render lands. A `findByRole` + explicit wait would make it pass reliably.
3. **Fixture pre-seeding race.** Setup helpers may create accounts/categories asynchronously and the test reads before mount completes.

## Suggested fix shape (S effort, file-wide pass)

Don't fix one test — fix the file's pattern. Sweep `frontend/tests/app/transactions-page.test.tsx` and replace EVERY `getByRole(/.../i)` that runs after a `render()` of `TransactionsPage` with `await findByRole(...)`. Or factor a helper at the top of the file that waits for the action column to be visible before any test assertions touch it (e.g., `await screen.findByRole('button', { name: /Edit:/i })` once after render, then sync `getByRole` calls for the rest of that test).

Run the entire file 30+ times in a row locally to gain confidence (`for i in {1..30}; do npx vitest run frontend/tests/app/transactions-page.test.tsx --reporter=json | grep numFailingTests; done`). Don't ship until you observe 0 flakes in that loop.

Don't change the production code — the responsive layout is correct in real browsers. Just fix the test file's brittleness, comprehensively.

## Touch points

- `frontend/tests/app/transactions-page.test.tsx` — file-wide audit + targeted async-wait conversions. Likely a shared setup helper at top of the file (renders TransactionsPage, returns a refs object) — that helper should `await` the first render-complete affordance before returning.
- Possibly `frontend/tests/setup.ts` if a shared mock has a deferred resolution pattern that's contributing.

## Impact

Every flaky failure costs a CI rerun (~7 min wall clock) and breaks the "merge as soon as green" flow. Has surfaced as PR-blocker friction on at least 4 PRs to date.

## Effort

XS-S. ~10-30 LoC fix, plus running the test 20-50 times locally to gain confidence in stability before pushing.
