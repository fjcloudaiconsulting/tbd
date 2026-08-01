/**
 * Make Recharts render measurable content under jsdom (TBD-287).
 *
 * ## Why this exists
 *
 * `ResponsiveContainer` measures its parent to decide its own size. jsdom has
 * no layout engine, so every element reports **0x0**, the container renders
 * nothing, and a chart under test produces **zero** `.recharts-bar-rectangle`
 * elements. The consequence is not a coverage statistic: chart `onClick`
 * handlers were unreachable from a test and would have stayed green if deleted.
 *
 * ## Why a shared helper rather than a per-file mock
 *
 * Before this, **14 test files** had each written their own `recharts` mock and
 * **13 of them were the weak version** — `<div>{children}</div>`, which passes
 * the child through without giving it dimensions, so the chart still renders
 * nothing. 12 of those 13 go further and replace `BarChart`/`Bar`/`Cell`
 * wholesale, so they assert against *their own stubs* rather than Recharts
 * output.
 *
 * Only `tests/app/dashboard-transfer-collapse.test.tsx` had the working shape,
 * derived by hand during the TBD-268 review fold. This helper is that shape,
 * named once.
 *
 * ## Usage
 *
 * `vi.mock` is hoisted above imports, so the factory cannot close over a
 * top-level import. Use a dynamic import **inside** the factory:
 *
 * ```tsx
 * vi.mock("recharts", async () => {
 *   const { rechartsWithFixedSize } = await import("@/tests/utils/recharts");
 *   return rechartsWithFixedSize();
 * });
 * ```
 *
 * Everything except `ResponsiveContainer` stays real, so `.recharts-*` class
 * assertions and real event handlers behave as they do in a browser.
 *
 * ## ⚠ Do NOT move this into `vitest.setup.ts`
 *
 * The reason is NOT "it would break the existing chart tests" — it would not.
 * A file-level `vi.mock` reliably overrides a setup-level one: the mock
 * registry is keyed by resolved module URL and the test file's hoisted
 * registration lands last. The 14 files above would each render exactly what
 * they render today, and 12 are structurally immune anyway because they replace
 * the module wholesale instead of spreading `importActual`.
 *
 * The real reason is the **other** set: every test file that renders a
 * chart-bearing tree *without* declaring its own `recharts` mock. Those
 * currently render nothing where a chart sits. A global stub would flip all of
 * them to full SVG trees — new nodes inside `getByText` / `queryAllBy*` scopes,
 * real `Tooltip`/`Cell` subtrees, and a per-test cost — in suites that never
 * asked for a chart. Opt in per file.
 *
 * ## Mechanism note (so a future breakage is diagnosable)
 *
 * The real `ResponsiveContainer` does **not** clone its child; it publishes
 * dimensions through a context provider. This stub instead clones with explicit
 * `width`/`height`, which works because Recharts' wrapper falls back to props
 * when the context reports no size. Equivalent in effect, different in
 * mechanism — if a future Recharts drops that props fallback, this stub stops
 * rendering while production stays fine.
 */
import * as React from "react";

/** Matches the fixture size measured in the TBD-268 fold (0 bars -> 2 bars). */
export const CHART_TEST_WIDTH = 400;
export const CHART_TEST_HEIGHT = 300;

/**
 * Spread the result into a `vi.mock("recharts", ...)` factory.
 *
 * Returns the real module with `ResponsiveContainer` replaced by a fixed-size
 * wrapper that clones its child with explicit `width`/`height` — which is the
 * part that matters. A wrapper that only renders `{children}` leaves the chart
 * at 0x0 and is indistinguishable, from the test's point of view, from having
 * no chart at all.
 */
export async function rechartsWithFixedSize(
  { width = CHART_TEST_WIDTH, height = CHART_TEST_HEIGHT } = {},
) {
  const actual = await vi.importActual<typeof import("recharts")>("recharts");
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: React.ReactElement }) => (
      <div style={{ width, height }}>
        {React.cloneElement(children, { width, height } as never)}
      </div>
    ),
  };
}
