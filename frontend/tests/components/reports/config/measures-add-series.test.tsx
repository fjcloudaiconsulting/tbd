/**
 * TBD-382 Defect B / ruling R7 — "+ Add series" seeds the next unused
 * catalog (agg, field) PAIR, and refuses when the catalog is exhausted.
 *
 * Fences F13, F14.
 *
 * The shipped bug seeded ``{agg:"sum", field: fields[0]}``. Series 1 typically
 * already IS that pair, so on line / area the second series draws
 * pixel-identical on top of the first — "the series does absolutely nothing".
 *
 * ⚠ There is deliberately NO agg-rotation fallback. `validate_against_catalog`
 * is not the only backend validator (CreditUtilizationSource enforces the PAIR
 * against an exhaustive `_DECLARED_AGG` map), rotation mints meaningless
 * measures like `SUM(transactions.id)`, and on `networth` it reproduces
 * Defect B verbatim because `build_rows` ignores `measure.agg`/`measure.field`
 * entirely. Exhausted means DISABLED.
 */
import { renderWithSWR, fireEvent, screen, waitFor } from "../../../utils/render-with-swr";

import DataTab from "@/components/reports/config/DataTab";
import { mockReportSources } from "../../../utils/mock-report-sources";
import type { LineWidget, SeriesConfig, Widget } from "@/lib/reports/types";

vi.mock("@/lib/api", () => ({
  apiFetch: (path: string) => mockReportSources()(path),
}));

function makeLine(
  dataset: LineWidget["config"]["dataset"],
  measures: SeriesConfig[],
): LineWidget {
  return {
    id: "w_line",
    type: "line",
    title: "Line",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset,
      measures,
      dimensions: ["month"],
    },
  };
}

async function addButton() {
  const btn = await screen.findByTestId("measure-add");
  // The catalog resolves asynchronously; R7 is defined in terms of catalog
  // pairs and has no meaning before they exist, so the button starts
  // disabled and only settles once /sources lands.
  await waitFor(() => expect(btn).toBeEnabled());
  return btn;
}

describe("R7 — '+ Add series' seeds the next unused catalog pair", () => {
  // ── F13 ───────────────────────────────────────────────────────────────
  it("F13: transactions [sum(amount)] seeds avg(amount), not a duplicate sum(amount)", async () => {
    const updates: Widget[] = [];
    renderWithSWR(
      <DataTab
        widget={makeLine("transactions", [
          { measure: { agg: "sum", field: "amount" } },
        ])}
        onUpdate={(w) => updates.push(w)}
      />,
    );

    fireEvent.click(await addButton());

    const next = updates.at(-1) as LineWidget;
    expect(next.config.measures).toHaveLength(2);
    expect(next.config.measures[1].measure).toEqual({
      agg: "avg",
      field: "amount",
    });
    // The seeded pair must not equal series 1.
    expect(next.config.measures[1].measure).not.toEqual(
      next.config.measures[0].measure,
    );
  });

  it("F13: networth publishes exactly one pair, so the button is DISABLED with one series", async () => {
    renderWithSWR(
      <DataTab
        widget={makeLine("networth", [
          { measure: { agg: "sum", field: "net_worth" } },
        ])}
        onUpdate={() => {}}
      />,
    );

    const btn = await screen.findByTestId("measure-add");
    await waitFor(() => expect(btn).toBeDisabled());
    // The reason is announced, not hidden in a title attribute.
    expect(
      await screen.findByTestId("measure-add-exhausted-help"),
    ).toBeInTheDocument();
  });

  // ── F14 ───────────────────────────────────────────────────────────────
  it("F14: [sum(amount), avg(amount)] seeds count(id) — de-duped against EVERY series, not just [0]", async () => {
    const updates: Widget[] = [];
    renderWithSWR(
      <DataTab
        widget={makeLine("transactions", [
          { measure: { agg: "sum", field: "amount" } },
          { measure: { agg: "avg", field: "amount" } },
        ])}
        onUpdate={(w) => updates.push(w)}
      />,
    );

    fireEvent.click(await addButton());

    const next = updates.at(-1) as LineWidget;
    expect(next.config.measures).toHaveLength(3);
    expect(next.config.measures[2].measure).toEqual({
      agg: "count",
      field: "id",
    });
  });

  it("F14: with all three transactions pairs present the button is DISABLED despite MAX_SERIES=5", async () => {
    renderWithSWR(
      <DataTab
        widget={makeLine("transactions", [
          { measure: { agg: "sum", field: "amount" } },
          { measure: { agg: "avg", field: "amount" } },
          { measure: { agg: "count", field: "id" } },
        ])}
        onUpdate={() => {}}
      />,
    );

    const btn = await screen.findByTestId("measure-add");
    // Still RENDERED (3 < MAX_SERIES) but refusing: an agg-rotation
    // fallback would leave it enabled and seed sum(id).
    expect(btn).toBeInTheDocument();
    await waitFor(() => expect(btn).toBeDisabled());
    expect(
      await screen.findByTestId("measure-add-exhausted-help"),
    ).toBeInTheDocument();
  });

  it("R7.3: the button is disabled while the source catalog has not resolved", () => {
    renderWithSWR(
      <DataTab
        widget={makeLine("transactions", [
          { measure: { agg: "sum", field: "amount" } },
        ])}
        onUpdate={() => {}}
      />,
    );
    // Synchronously — before /sources resolves — there are no catalog pairs,
    // so there is no such thing as "the next unused pair".
    expect(screen.getByTestId("measure-add")).toBeDisabled();
    // …and no exhausted reason either: unknown is not the same as exhausted.
    expect(screen.queryByTestId("measure-add-exhausted-help")).toBeNull();
  });

  it("R7: the add button carries a visible focus state (DESIGN.md Pressable-Surfaces Rule)", async () => {
    renderWithSWR(
      <DataTab
        widget={makeLine("transactions", [
          { measure: { agg: "sum", field: "amount" } },
        ])}
        onUpdate={() => {}}
      />,
    );
    const btn = await screen.findByTestId("measure-add");
    expect(btn.className).toContain("focus-visible:ring-2");
    expect(btn.className).toContain("focus-visible:ring-accent/30");
    expect(btn.className).toContain("disabled:cursor-not-allowed");
    expect(btn.className).toContain("disabled:opacity-60");
  });
});
