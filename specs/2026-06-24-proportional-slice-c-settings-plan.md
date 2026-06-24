# Proportional Pass — Slice C (Settings normalize) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Normalize the Settings tabs to one consistent, proportional width. Profile, Security, and Notifications are hard-capped at `max-w-lg` (512px) — marooned in white space at 1760px — while Organization and AI Providers already fill width. Remove the caps and give the three form tabs a proportional 2-column card layout (form fields stay a sensible width inside their column).

**Architecture:** Each Settings tab is its own route page rendered inside the shared `SettingsLayout` (tabs nav). The narrow cap is a per-page `<div className="max-w-lg space-y-6">` wrapper. Replace it with a proportional 2-column card grid. Pure layout — no data/behavior change. Org + AI Providers already fill width → leave; just confirm consistency.

**Tech Stack:** Next.js 16 / React 19 / TS / Tailwind v4, Vitest.

## Global Constraints

- **No Off-Token Rule** — token color classes only; CI-gated. `grid`/`col-span`/`gap`/`max-w-[*]` are layout/size utils (allowed).
- **Frontend verify INCLUDES `npm run lint`** (eslint `no-explicit-any` CI-gated, not caught by tsc/tests) → [[reference_eslint_ci_gate_misses]]. No `as any`.
- **No AI attribution** in commits or PR body → [[feedback_no_ai_attribution]].
- Form `input` styling comes from `lib/styles.ts` (`input`); inputs that need a cap use `max-w-md` (existing pattern, e.g. organization page).
- Tests in the frontend container: `docker compose exec frontend <cmd>`. Branch `feat/proportional-c-settings` (off main; has #478 width + #479/#480 shipped).

## Principle for the three narrow tabs

Remove `max-w-lg`. Use `grid grid-cols-1 lg:grid-cols-2 gap-6` so the tab's cards sit two-up on wide screens and stack on narrow — proportional, never marooned, never a single 1760px-wide form row. Keep individual form inputs at a readable width within their column (`max-w-md` where an input would otherwise stretch the whole column).

---

### Task 1: Profile tab — proportional 2-col (the template)

**Files:**
- Modify: `frontend/app/settings/page.tsx` (content wrapper ~line 199)
- Test: grep `frontend/tests/` for a settings/profile test; add/extend a focused layout assertion.

- [ ] **Step 1: READ `frontend/app/settings/page.tsx`** from ~line 199 to the end of the returned content. Inventory the cards: the SSO step-up banner (conditional, full-width — keep above the grid), the identity/avatar card, the Edit-Profile form card, and the Dashboard-Tour card. Note their existing markup/classes.

- [ ] **Step 2: Replace the cap with a proportional layout.** Change `<div className="max-w-lg space-y-6">` to a structure where: any full-width banners stay full-width at top; then a `grid grid-cols-1 lg:grid-cols-2 gap-6` holds the cards — **left column:** identity card + Edit-Profile form card (wrapped in their own `space-y-6` div); **right column:** Dashboard-Tour card (+ room for future). Keep each card's existing inner markup. Ensure form inputs inside the Edit-Profile card use the existing field styling with a sane width (they already render inside a ~500px card today; in an ~860px column, add `max-w-md` to any input that would otherwise span the full column, matching the organization page pattern).

- [ ] **Step 3: Typecheck + lint + tests.** `docker compose exec frontend npx tsc --noEmit && docker compose exec frontend npm run lint && docker compose exec frontend npm test`. Green. If a profile/settings test asserts the `max-w-lg` wrapper or old structure, update it (same cards/fields present).

- [ ] **Step 4: Commit.**
```bash
git add frontend/app/settings/page.tsx frontend/tests/
git commit -m "feat(settings): proportional 2-col Profile tab (drop max-w-lg)"
```

---

### Task 2: Security + Notifications tabs — same proportional pattern

**Files:**
- Modify: `frontend/app/settings/security/page.tsx` (wrapper ~line 422), `frontend/app/settings/notifications/page.tsx` (wrapper ~line 116)
- Test: extend the relevant settings tests if they assert the old wrapper.

- [ ] **Step 1: READ both files.** Security (~422): inventory its cards (password change, MFA/2FA with the `h-48 w-48` QR, active sessions, SSO step-up, etc.). Notifications (~116): inventory its content (category preference sections/cards + per-category toggles).

- [ ] **Step 2: Security → 2-col.** Replace `<div className="max-w-lg space-y-6">` with `grid grid-cols-1 lg:grid-cols-2 gap-6` (or a left/right split if a card is naturally tall, e.g. MFA-with-QR in one column and password+sessions in the other — implementer's judgment for balance). Keep the QR at its fixed `h-48 w-48`. Inputs keep `w-full sm:max-w-[200px]` etc. as-is. Any full-width banner stays above the grid.

- [ ] **Step 3: Notifications → proportional.** Replace `max-w-lg`. If Notifications renders multiple category cards/sections, put them in `grid grid-cols-1 lg:grid-cols-2 gap-6` so toggle rows aren't absurdly wide at 1760px. If it's a single monolithic card, instead cap that card at a comfortable reading width (`max-w-3xl`) so toggle rows stay legible — pick whichever the actual structure makes cleanest, and prefer the 2-col grid for consistency with Profile/Security. Keep the `max-w-sm` helper-text caps.

- [ ] **Step 4: Typecheck + lint + full suite.** Same commands as Task 1 Step 3. Update any test asserting the old wrappers.

- [ ] **Step 5: Commit.**
```bash
git add frontend/app/settings/security/page.tsx frontend/app/settings/notifications/page.tsx frontend/tests/
git commit -m "feat(settings): proportional Security + Notifications tabs (drop max-w-lg)"
```

---

### Task 3: Verify Org + AI Providers unaffected + visual pass

**Files:** none (verification).

- [ ] **Step 1: Confirm** `settings/organization/page.tsx` and `settings/ai-providers/page.tsx` have NO outer `max-w-lg`/narrow cap (they already fill width; the `max-w-md` on an org input and the `max-w-md` on the AI add-credential modal are intentional component caps — leave them). No change needed; just confirm they read consistently wide with the now-normalized tabs.
- [ ] **Step 2: Visual check** at 1760px: switch across all five tabs (Profile / Security / Notifications / Organization / AI Providers) — all now use the width consistently; no tab is marooned narrow; the three form tabs read as proportional 2-col card layouts; form inputs aren't stretched to absurd widths.
- [ ] **Step 3: Mobile (390px):** all tabs stack to a single column; no horizontal scroll; tab nav still works.

## Self-review (done)
- **Spec coverage:** the Proportional Pass spec's Settings section — remove `max-w-lg` from Profile/Security/Notifications, normalize UP to proportional 2-col (matching the wide Org/AI tabs); inputs capped within columns. Profile (T1) is the template; Security + Notifications (T2) replicate; Org/AI verified unchanged (T3).
- **Placeholders:** per-tab card inventories are read from the live files (T1S1, T2S1) before editing — concrete.
- **Type consistency:** N/A (layout-only; no new shared API). Reuses `input` from `lib/styles.ts`.
- **Note:** three tabs were capped (Profile, Security, AND Notifications) — the audit initially flagged only Profile/Security; Notifications `:116` also has `max-w-lg`, included here.
