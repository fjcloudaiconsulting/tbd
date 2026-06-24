# W4 Phase 0 — Wider Canvas (app-wide) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (this phase is a one-line global change + verification; inline execution is proportionate). Steps use checkbox (`- [ ]`) syntax.

**Goal:** Widen the app's content area for every page from `max-w-screen-xl` (1280px) to `max-w-[1760px]`, so large monitors stop wasting space and the dashboard/reports canvases have more room to arrange.

**Architecture:** Single change to the AppShell main-content wrapper. No per-route logic (operator chose app-wide). Grid column count unchanged (12), so existing saved Report layouts keep valid coordinates.

**Tech Stack:** Next.js 16 / React 19 / Tailwind v4.

## Global Constraints

- **No Off-Token Rule** — only colors are CI-blocked by `frontend/scripts/check-design-tokens.sh`; `max-w-[1760px]` is an arbitrary *size* utility (not a color) and is allowed.
- **Frontend verify includes `npm run lint`** (eslint `no-explicit-any` is CI-gated, not caught by `tsc`/tests) → [[reference_eslint_ci_gate_misses]].
- **No AI attribution** in commits or PR body → [[feedback_no_ai_attribution]].
- Tests/checks run in the frontend container: `docker compose exec frontend <cmd>`.

---

### Task 1: Widen the AppShell content cap app-wide

**Files:**
- Modify: `frontend/components/AppShell.tsx:620` (the `<main>` content wrapper)

- [ ] **Step 1: Make the change.** At `AppShell.tsx:620`, change the content wrapper's max-width from `max-w-screen-xl` to `max-w-[1760px]`:
```jsx
<main id="main-content" tabIndex={-1} className="flex-1 overflow-auto p-4 sm:p-8"><div className="mx-auto max-w-[1760px]">{children}</div></main>
```
(keep `mx-auto`, `p-4 sm:p-8`, and the rest of the line identical).

- [ ] **Step 2: Confirm there is no OTHER competing cap.** Run: `docker compose exec -T frontend grep -rn "max-w-screen-xl" app/ components/ | grep -v node_modules`. Expected: any remaining hits are intentional local caps (e.g. an empty-state `max-w-md`), NOT a second global content wrapper. If another global content wrapper exists, widen it too; report what you found.

- [ ] **Step 3: Design-token gate.** Run: `docker compose exec -T frontend bash scripts/check-design-tokens.sh`. Expected: exit 0 (size utility, not a color).

- [ ] **Step 4: Typecheck + lint + full suite.** Run: `docker compose exec -T frontend npx tsc --noEmit && docker compose exec -T frontend npm run lint && docker compose exec -T frontend npm test`. Expected: tsc clean, lint 0 errors, full suite green. (No test should assert on the old `max-w-screen-xl` class; if one does, update it — grep `max-w-screen-xl` under `tests/`.)

- [ ] **Step 5: Visual sanity check.** With the dev stack running, open the app on a >1280px viewport and confirm `/dashboard`, `/reports`, and a text page (`/settings` or `/transactions`) all fill to ~1760px centered with slim gutters, and there's no horizontal page scroll at desktop or mobile widths.

- [ ] **Step 6: Commit.**
```bash
git add frontend/components/AppShell.tsx
git commit -m "feat(ui): widen app content area to 1760px (W4 Phase 0)"
```

---

## Self-review (done)

- **Spec coverage:** Phase 0 of the W4 spec = global widen to ~1760px for all routes. Task 1 implements exactly that. No per-route mechanism (operator decision).
- **Placeholders:** none — the exact className change and exact commands are given.
- **Type consistency:** N/A (single CSS class change).
- **Risk:** a second global content wrapper would mean the widen looks partial — Step 2 explicitly checks for that.
