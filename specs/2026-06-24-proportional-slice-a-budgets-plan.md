# Proportional Pass — Slice A (StatCard + Budgets) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add a shared `StatCard` primitive and use it + a proportional 2-column layout to de-stretch the Budgets page at the new 1760px width (KPIs row, then Overview chart and Details side-by-side instead of two stacked full-width bands).

**Architecture:** New `StatCard` component replaces hand-rolled KPI tiles. Budgets page rearranges into a responsive grid; the horizontal bar chart is contained to ~60% width beside the details list, so nothing spans the full 1760px alone. Pure layout — no data/behavior change.

**Tech Stack:** Next.js 16 / React 19 / TS / Tailwind v4 / Recharts, Vitest.

## Global Constraints

- **No Off-Token Rule** — token color classes only (`text-success`, `text-danger`, `text-text-*`, `bg-surface`, …); CI-gated by `frontend/scripts/check-design-tokens.sh`. `grid-cols-*`/`gap-*`/`col-span-*`/`max-w-[*]` are layout/size utilities (allowed).
- **Frontend verify INCLUDES `npm run lint`** (eslint `no-explicit-any` is CI-gated, not caught by tsc/tests) → [[reference_eslint_ci_gate_misses]]. Never introduce `as any`.
- **No AI attribution** in commits or PR body → [[feedback_no_ai_attribution]].
- Card primitives come from `frontend/lib/styles.ts` (`card`, `cardTitle`); reuse them, don't reinvent.
- Tests run in the frontend container: `docker compose exec frontend <cmd>`. Branch: `feat/proportional-a-budgets` (off main; has the 1760px width).

---

### Task 1: `StatCard` shared primitive

**Files:**
- Create: `frontend/components/ui/StatCard.tsx`
- Test: `frontend/tests/components/ui/stat-card.test.tsx`

**Interfaces:**
- Produces (consumed by Task 2 + later slices):
```ts
interface StatCardProps {
  label: string;
  value: React.ReactNode;
  valueClassName?: string;   // token color util for the value, e.g. "text-success"
  sub?: React.ReactNode;     // optional secondary line
  badge?: React.ReactNode;   // optional pill/status node
}
export default function StatCard(props: StatCardProps): JSX.Element
```

- [ ] **Step 1: READ the current Budgets KPI markup** (`frontend/app/budgets/page.tsx` ~lines 362-384) to capture the exact label/value typography + spacing, so `StatCard` is visually identical to today's tiles. Note the classes used (label uppercase/muted, value size/weight, `card` wrapper, padding e.g. `p-5`).

- [ ] **Step 2: Write the failing test.** `frontend/tests/components/ui/stat-card.test.tsx`:
```tsx
import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import StatCard from "@/components/ui/StatCard";

describe("StatCard", () => {
  it("renders label, value, sub and applies valueClassName", () => {
    render(<StatCard label="TOTAL BUDGET" value="2,300.00" sub="Actual: 0.00" valueClassName="text-success" />);
    expect(screen.getByText("TOTAL BUDGET")).toBeInTheDocument();
    const val = screen.getByText("2,300.00");
    expect(val).toBeInTheDocument();
    expect(val.className).toContain("text-success");
    expect(screen.getByText("Actual: 0.00")).toBeInTheDocument();
  });
  it("omits sub/badge when not provided", () => {
    const { container } = render(<StatCard label="X" value="1" />);
    expect(screen.getByText("X")).toBeInTheDocument();
    expect(container.textContent).toContain("1");
  });
});
```

- [ ] **Step 3: Run it, verify it fails.** Run: `docker compose exec frontend npm test -- tests/components/ui/stat-card.test.tsx`. Expected: FAIL (module not found).

- [ ] **Step 4: Implement `StatCard`** mirroring the captured Budgets KPI styling, e.g.:
```tsx
import { card, cardTitle } from "@/lib/styles";

interface StatCardProps {
  label: string;
  value: React.ReactNode;
  valueClassName?: string;
  sub?: React.ReactNode;
  badge?: React.ReactNode;
}

export default function StatCard({ label, value, valueClassName, sub, badge }: StatCardProps) {
  return (
    <div className={`${card} p-5`}>
      <div className="flex items-center justify-between">
        <div className={cardTitle}>{label}</div>
        {badge}
      </div>
      <div className={`mt-2 text-2xl font-semibold ${valueClassName ?? "text-text-primary"}`}>{value}</div>
      {sub ? <div className="mt-1 text-sm text-text-muted">{sub}</div> : null}
    </div>
  );
}
```
(Adjust class names to EXACTLY match the captured current tile so the visual is unchanged — the Step 1 read is authoritative over this sketch.)

- [ ] **Step 5: Run test, verify it passes.** Then `docker compose exec frontend npx tsc --noEmit`.

- [ ] **Step 6: Commit.**
```bash
git add frontend/components/ui/StatCard.tsx frontend/tests/components/ui/stat-card.test.tsx
git commit -m "feat(ui): shared StatCard primitive for KPI tiles"
```

---

### Task 2: Budgets — StatCard KPIs + proportional chart/details layout

**Files:**
- Modify: `frontend/app/budgets/page.tsx`
- Test: extend/confirm `frontend/tests/app/budgets-*.test.tsx` if one exists (grep `tests/` for budgets); otherwise add a focused layout test.

**Interfaces:**
- Consumes: `StatCard` (Task 1).

- [ ] **Step 1: READ `frontend/app/budgets/page.tsx`** — locate the 3 KPI tiles (~362-384), the Budget Overview chart card (~386-408), and the Details card (~412-487). Confirm the data vars feeding each.

- [ ] **Step 2: Swap KPIs to `StatCard`.** Replace the three inline KPI divs with `<StatCard ...>` inside the existing `grid grid-cols-1 sm:grid-cols-3 gap-4` row (Total Budget, Total Spent, Remaining — Remaining keeps its success color via `valueClassName="text-success"`; match current colors exactly). Behavior/values unchanged.

- [ ] **Step 3: Proportional 2-col for chart + details.** Wrap the Budget Overview chart card and the Details card in a single grid so they sit side-by-side on wide screens and stack on narrow:
```tsx
<div className="grid grid-cols-1 xl:grid-cols-5 gap-6">
  <div className="xl:col-span-3"> {/* Budget Overview chart card (existing card markup) */} </div>
  <div className="xl:col-span-2"> {/* Details card (existing card markup) */} </div>
</div>
```
Keep each card's existing inner markup/classes; only the wrapper/column-span is new. The Recharts `ResponsiveContainer` keeps `w-full` and now fills the ~60% column instead of the full page.

- [ ] **Step 4: Guard the chart height.** Confirm the chart card has a defined height (Recharts `ResponsiveContainer` needs it). It already renders today, so keep the existing height; just verify the narrower column doesn't collapse it (the height is fixed/row-based, not width-derived).

- [ ] **Step 5: Typecheck + lint + tests.** Run: `docker compose exec frontend npx tsc --noEmit && docker compose exec frontend npm run lint && docker compose exec frontend npm test`. Expected: tsc clean, lint 0 errors, suite green. If a budgets test asserts old KPI DOM structure, update it to the `StatCard` output (values/labels still present).

- [ ] **Step 6: Commit.**
```bash
git add frontend/app/budgets/page.tsx frontend/tests/
git commit -m "feat(budgets): proportional layout — StatCard KPIs + chart/details 2-col"
```

---

### Task 3: Visual verification

- [ ] **Step 1.** With the stack running on this branch, open `/budgets` at 1760px: KPIs are an even 3-across row; the Overview chart sits at ~60% beside the Details list at ~40%; the bar chart no longer spans the full width with a huge empty right side.
- [ ] **Step 2.** At a tablet width (`<xl`) and mobile (390px): chart and details stack into a single column; KPIs collapse per the existing responsive grid; no horizontal scroll.
- [ ] **Step 3.** Confirm the cards look identical to before (same borders/padding/typography) — only their arrangement changed.

## Self-review (done)
- **Spec coverage:** `StatCard` primitive (T1) + Budgets proportional layout (T2: KPIs→StatCard, chart|details 2-col) + visual check (T3). Matches the Proportional Pass spec's Slice A.
- **Placeholders:** the `StatCard` implementation sketch is explicitly subordinate to the Step-1 read of the current tile (so the look is unchanged) — concrete, not a TODO.
- **Type consistency:** `StatCardProps` (label/value/valueClassName/sub/badge) used identically in T1 and T2.
