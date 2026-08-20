/**
 * TBD-382 R13 / F20 — there is exactly ONE widget factory.
 *
 * `app/reports/[id]/page.tsx` used to carry its own byte-identical copy of
 * `emptyKPI` / `emptyBar` / `emptyMultiSeries` / `emptyWidget`, and it was
 * LIVE: the saved-report editor called the local copy while `/reports/new`
 * called widgetKit's. Any change to the `stacked_bar` seed applied to one
 * module only means the two editors create different widgets — this repo's
 * signature half-fix-leaves-a-door shape.
 *
 * Both factory sites also seeded `sort: {by:"value", dir:"desc"}` on a `month`
 * primary. Under R3 branch 3 that renders a newly-created stacked bar's month
 * axis in spend order, which R9 calls out as never the intended reading of a
 * time axis. R9 fixes the template and R3 branch 1 fixes already-cloned
 * reports; the factory is the third site and nothing else covers it.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { emptyWidget } from "@/components/reports/widgetKit";
import type { StackedBarWidget, TableWidget } from "@/lib/reports/types";

const PAGE_SOURCE = join(
  process.cwd(),
  "app",
  "reports",
  "[id]",
  "page.tsx",
);

describe("F20 — one widget factory, seeding a chronological time axis", () => {
  it("seeds stacked_bar with a single measure and an ASCENDING dimension sort", () => {
    const w = emptyWidget("stacked_bar", "w_1") as StackedBarWidget;
    expect(w.type).toBe("stacked_bar");
    expect(w.config.dimensions).toEqual(["month"]);
    expect(w.config.measures).toHaveLength(1);
    // A time primary is never read in spend order.
    expect(w.config.sort).toEqual({ by: "dimension", dir: "asc" });
  });

  it.each(["line", "area"] as const)(
    "seeds %s (also a month primary) with the same ascending dimension sort",
    (type) => {
      const w = emptyWidget(type, "w_1");
      expect((w.config as StackedBarWidget["config"]).sort).toEqual({
        by: "dimension",
        dir: "asc",
      });
    },
  );

  it("leaves the table seed on value-desc — its primary is `category`, not a time bucket", () => {
    const w = emptyWidget("table", "w_1") as TableWidget;
    expect(w.config.dimensions).toEqual(["category"]);
    expect(w.config.sort).toEqual({ by: "value", dir: "desc" });
  });

  it("the saved-report editor has NO local factory: it imports widgetKit's", () => {
    const src = readFileSync(PAGE_SOURCE, "utf8");
    // Sanity: we actually read the page, not an empty string.
    expect(src).toContain("export default function ReportEditorPage");

    // The duplicate is gone…
    expect(src).not.toMatch(/function\s+emptyMultiSeries\s*\(/);
    expect(src).not.toMatch(/function\s+emptyWidget\s*\(/);
    expect(src).not.toMatch(/function\s+emptyKPI\s*\(/);
    expect(src).not.toMatch(/function\s+emptyBar\s*\(/);
    expect(src).not.toMatch(/function\s+emptyPie\s*\(/);
    expect(src).not.toMatch(/function\s+emptySparkline\s*\(/);
    expect(src).not.toMatch(/function\s+emptySankey\s*\(/);
    expect(src).not.toMatch(/function\s+newWidgetId\s*\(/);

    // …and the shared one is what the page uses.
    expect(src).toMatch(
      /import\s*\{[^}]*\bemptyWidget\b[^}]*\}\s*from\s*"@\/components\/reports\/widgetKit"/s,
    );
    expect(src).toMatch(
      /import\s*\{[^}]*\bnewWidgetId\b[^}]*\}\s*from\s*"@\/components\/reports\/widgetKit"/s,
    );
  });
});
