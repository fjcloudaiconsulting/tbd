# Proportional Pass — Slice B (Forecast Plans) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** De-stretch the Forecast Plans page at the 1760px width: KPIs use the shared `StatCard` (extended with `valueSize`/`subClassName`), the "Planned vs Actual" chart is contained instead of spanning the full width, and the detail tables stay full-width.

**Architecture:** Extend `StatCard` with two optional, backward-compatible props so it fits Forecast Plans' `text-xl` tiles. Swap the 4 hand-rolled KPI tiles to `StatCard`. Contain the chart in a proportional grid (chart beside a compact legend/summary) so it no longer spans ~1760px alone; leave the income/expense detail tables full-width (tables use width well). Pure layout — no data/behavior change.

**Tech Stack:** Next.js 16 / React 19 / TS / Tailwind v4 / Recharts, Vitest.

## Global Constraints

- **No Off-Token Rule** — token color classes only; CI-gated. `grid`/`col-span`/`gap`/`max-w-[*]` are layout/size utils (allowed).
- **Frontend verify INCLUDES `npm run lint`** (eslint `no-explicit-any` is CI-gated; not caught by tsc/tests) → [[reference_eslint_ci_gate_misses]]. Never `as any`.
- **No AI attribution** in commits or PR body → [[feedback_no_ai_attribution]].
- `StatCard` (`frontend/components/ui/StatCard.tsx`) shipped in Slice A (#479) — props `{ label, value, valueClassName?, sub?, badge? }`, value default `text-2xl`, sub default `mt-1 text-sm text-text-muted`, sub element carries `data-testid="stat-card-sub"`.
- Tests in the frontend container: `docker compose exec frontend <cmd>`. Branch: `feat/proportional-b-forecast-plans` (off main, has #478 width + #479 StatCard).

---

### Task 1: Extend `StatCard` with `valueSize` + `subClassName`

**Files:**
- Modify: `frontend/components/ui/StatCard.tsx`
- Modify: `frontend/tests/components/ui/stat-card.test.tsx`

**Interfaces:**
- Produces (consumed by Task 2):
```ts
interface StatCardProps {
  label: string;
  value: React.ReactNode;
  valueClassName?: string;
  valueSize?: string;     // NEW — value text size util; default "text-2xl"
  sub?: React.ReactNode;
  subClassName?: string;  // NEW — sub line classes; default "mt-1 text-sm text-text-muted"
  badge?: React.ReactNode;
}
```

- [ ] **Step 1: Write failing tests.** Extend `stat-card.test.tsx`:
```tsx
it("defaults value size to text-2xl and applies a custom valueSize", () => {
  const { rerender } = render(<StatCard label="A" value="1" />);
  expect(screen.getByText("1").className).toContain("text-2xl");
  rerender(<StatCard label="A" value="2" valueSize="text-xl" />);
  const v = screen.getByText("2");
  expect(v.className).toContain("text-xl");
  expect(v.className).not.toContain("text-2xl");
});
it("applies a custom subClassName when sub is provided", () => {
  render(<StatCard label="A" value="1" sub="Actual: 0.00" subClassName="mt-0.5 text-xs text-text-muted" />);
  const sub = screen.getByTestId("stat-card-sub");
  expect(sub.className).toContain("text-xs");
});
```

- [ ] **Step 2: Run, verify fail.** `docker compose exec frontend npm test -- tests/components/ui/stat-card.test.tsx` → FAIL (default text-2xl not parameterized / subClassName ignored).

- [ ] **Step 3: Implement.** Add the two props; in the value `<p>` use `${valueSize ?? "text-2xl"}` where the size currently is hardcoded; in the sub `<p>` use `className={subClassName ?? "mt-1 text-sm text-text-muted"}` (keep `data-testid="stat-card-sub"`). Keep `font-semibold tabular-nums` and the `valueClassName` color fallback intact. Budgets call sites pass neither new prop → unchanged.

- [ ] **Step 4: Run, verify pass.** Then `docker compose exec frontend npx tsc --noEmit` + confirm the existing Budgets render is unaffected (run `tests/app/budgets-layout.test.tsx` + `tests/components/ui/stat-card.test.tsx`).

- [ ] **Step 5: Commit.**
```bash
git add frontend/components/ui/StatCard.tsx frontend/tests/components/ui/stat-card.test.tsx
git commit -m "feat(ui): StatCard valueSize + subClassName props (for Forecast Plans tiles)"
```

---

### Task 2: Forecast Plans — StatCard KPIs + contained chart

**Files:**
- Modify: `frontend/app/forecast-plans/ForecastPlansClient.tsx`
- Test: grep `frontend/tests/` for forecast-plans tests; add a focused layout assertion or extend.

**Interfaces:**
- Consumes: extended `StatCard` (Task 1).

- [ ] **Step 1: READ `ForecastPlansClient.tsx`** around the KPI row (~line 1148-1185: `grid grid-cols-2 gap-4 lg:grid-cols-4`, four tiles — Planned Income `text-success`, Planned Expenses `text-danger`, Planned Net `plannedNet>=0?success:danger`, Actual Net `actualNet>=0?success:danger`, each with an `Actual: {formatAmount(...)}` sub line) and the chart block (~line 1190-1200, `ForecastPlanChart` dynamic). Capture the exact sub-line classes so `subClassName` reproduces them.

- [ ] **Step 2: Swap KPIs to `StatCard`.** Replace the four inline tiles with `<StatCard valueSize="text-xl" subClassName="<captured sub classes>" ...>` inside the existing `grid grid-cols-2 gap-4 lg:grid-cols-4` row. Preserve EXACT labels, value expressions, the success/danger conditional `valueClassName`, and the `Actual: …` sub for each. No value/condition change.

- [ ] **Step 3: Contain the chart.** Wrap the "Planned vs Actual" chart card so it no longer spans the full 1760px alone. Use `grid grid-cols-1 xl:grid-cols-3 gap-6`: chart card `xl:col-span-2` (≈66%), and beside it (`xl:col-span-1`) a compact card holding the chart's legend/summary OR the existing All/Expenses/Income view toggle + counts if they currently sit elsewhere — read what's adjacent and choose the cleanest. If there's no natural side content, cap the chart card at `xl:col-span-2` and leave the third column empty on `xl` (the chart still reads better at 66% than 100%). Keep the chart's existing fixed/defined height. Stacks to single column below `xl`.

- [ ] **Step 4: Leave detail tables full-width.** The Income/Expense category tables (and the All/Expenses/Income tab filter above them) stay full-width below the chart — do NOT constrain them (wide tables use width well). Confirm they're outside the chart's containment grid.

- [ ] **Step 5: Typecheck + lint + tests.** `docker compose exec frontend npx tsc --noEmit && docker compose exec frontend npm run lint && docker compose exec frontend npm test`. tsc clean, lint 0 errors, suite green. If a forecast-plans test asserts old KPI DOM, update to the StatCard output (same labels/values/colors/subs present).

- [ ] **Step 6: Commit.**
```bash
git add frontend/app/forecast-plans/ForecastPlansClient.tsx frontend/tests/
git commit -m "feat(forecast-plans): proportional layout — StatCard KPIs + contained chart"
```

---

### Task 3: Visual verification
- [ ] **Step 1.** `/forecast-plans` at 1760px: 4 KPIs even across; the Planned-vs-Actual chart contained (~66%) not spanning full width; detail tables full-width below; nothing disproportionate.
- [ ] **Step 2.** `<xl` + mobile (390px): KPIs collapse (2-col → 1), chart + side content stack, tables scroll/stack as before; no horizontal page scroll.
- [ ] **Step 3.** Cards look identical to before (only arrangement + the chart's width changed). Budgets page still correct (StatCard change is backward-compatible).

## Self-review (done)
- **Spec coverage:** StatCard extension (T1) unblocks the `text-xl`/`text-xs` Forecast tiles; Forecast Plans KPIs→StatCard + contained chart + full-width tables (T2); visual (T3). Matches the Proportional Pass spec's Slice B + closes the deferred #479 StatCard-props nit.
- **Placeholders:** sub-line classes are captured from the live file in T2 Step 1 (authoritative), not guessed.
- **Type consistency:** `valueSize`/`subClassName` names identical across T1 interface and T2 usage; defaults preserve Budgets behavior.
