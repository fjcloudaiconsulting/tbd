/**
 * TBD-381 — the filter controls must not lie.
 *
 * Owner-reported: "sparkline allows me to select net worth data source, but
 * still offers me transaction filters that if I use do not produce any result".
 *
 * ⚠ The symptom was UNDERSTATED, and these fences assert both halves, because
 * the two failure modes are different and only one of them is silent:
 *
 *   * `category_id` IS in `SHARED_CANVAS_FILTER_FIELDS`, so the backend drops
 *     it at build time -> the silent no-op the owner saw.
 *   * `txn_type` and `tag_name` are NOT shared-canvas fields, so
 *     `validate_against_catalog` RAISES -> a 422 surfaced as a bare
 *     "Couldn't load" with no explanation. A hard break, not a no-op.
 *
 * And it lied in the other direction too: `recurring` publishes an `amount`
 * filter that the editor HID, because the gate was
 * `allowTransfer = dataset === "transactions"` rather than the catalog.
 */
import { describe, expect, it, vi } from "vitest";

import { renderWithSWR, screen, waitFor } from "../../../utils/render-with-swr";
import {
  ALL_ENTRIES,
  mockReportSources,
} from "../../../utils/mock-report-sources";

import FilterEditor from "@/components/reports/config/FilterEditor";
import type { Dataset } from "@/lib/reports/types";

vi.mock("@/lib/api", () => ({
  // The catalog drives control visibility; accounts/categories/tags are
  // fetched by the individual controls and can be empty for this test --
  // we assert which controls RENDER, not what they contain.
  apiFetch: vi.fn(async (path: string) => {
    if (path.startsWith("/api/v1/reports/sources")) {
      return mockReportSources(ALL_ENTRIES)(path);
    }
    return [];
  }),
}));

// AccountFilter reads the auth context; mirror FilterEditor.test.tsx.
vi.mock("@/components/auth/AuthProvider", () => ({
  useAuth: () => ({ user: { id: 1 }, loading: false }),
}));

function renderFor(dataset: Dataset) {
  return renderWithSWR(
    <FilterEditor
      filters={{}}
      canvasFilters={{}}
      dataset={dataset}
      onChange={() => {}}
    />,
  );
}

describe("FilterEditor is driven by the source catalog", () => {
  it("net worth offers neither the silently-dropped nor the 422-ing controls", async () => {
    renderFor("networth" as Dataset);
    // Published by networth -> must be offered.
    await waitFor(() => expect(screen.getByText("Accounts")).toBeInTheDocument());

    // Silent no-op before: shared-canvas field, dropped at build time.
    expect(screen.queryByTestId("category-picker")).not.toBeInTheDocument();
    // Hard 422 before: NOT shared-canvas, so validate_against_catalog raises.
    expect(screen.queryByText("Transaction type")).not.toBeInTheDocument();
    expect(screen.queryByText("Status")).not.toBeInTheDocument();
    // Amount is not published by networth either.
    expect(screen.queryByLabelText("Widget amount min")).not.toBeInTheDocument();
    // ⚠ The half this fence originally MISSED while its own header claimed it
    // covered: `tag_name` is published only by transactions and is not a
    // shared-canvas field, so a tag picked here 422s the widget.
    expect(screen.queryByTestId("tag-filter")).not.toBeInTheDocument();
  });

  it("credit utilization offers no date control, because the source is point-in-time", async () => {
    renderFor("credit_utilization" as Dataset);
    await waitFor(() => expect(screen.getByText("Accounts")).toBeInTheDocument());
    // credit_utilization publishes NO date filter and says so in its source.
    expect(screen.queryByText("Date range")).not.toBeInTheDocument();
    expect(screen.queryByTestId("tag-filter")).not.toBeInTheDocument();
  });

  it("recurring GAINS the amount control it always supported", async () => {
    // The inverse bug: the old gate was `dataset === "transactions"`, so a
    // source publishing `amount` had the control hidden from it.
    renderFor("recurring" as Dataset);
    await screen.findByTestId("category-picker");
    // recurring publishes `amount` (kind "number"); the old gate hid it.
    expect(screen.getByLabelText("Widget amount min")).toBeInTheDocument();
    expect(screen.getByLabelText("Widget amount max")).toBeInTheDocument();
  });

  it("transactions still offers the full set", async () => {
    // The control. Without it, "render nothing" passes every test above.
    renderFor("transactions" as Dataset);
    await screen.findByTestId("category-picker");
    expect(screen.getByText("Date range")).toBeInTheDocument();
    expect(screen.getByText("Accounts")).toBeInTheDocument();
    expect(screen.getByText("Transaction type")).toBeInTheDocument();
    expect(screen.getByText("Status")).toBeInTheDocument();
    expect(screen.getByLabelText("Widget amount min")).toBeInTheDocument();
  });
});

describe("the empty-catalog bias is load-bearing", () => {
  it("offers everything when the catalog has not loaded", async () => {
    // ⚠ KILLS inverting the unknown-source default. If an empty catalog meant
    // "publish nothing", a cold SWR cache would strip every control from every
    // widget and silently render unfiltered totals -- a worse failure than the
    // one this ticket fixes, and one no user would report as a filter bug.
    renderWithSWR(
      <FilterEditor
        filters={{}}
        canvasFilters={{}}
        dataset={"networth" as Dataset}
        onChange={() => {}}
      />,
    );
    // ⚠ Assert controls the LOADED catalog would DENY. The earlier version
    // asserted "Date range" on networth -- which networth publishes -- so it
    // passed identically whether the allow-all branch ran or not. These two
    // are absent from networth's published set, so their presence PROVES the
    // pre-load branch ran. Kills a half-inversion like
    //   `if (!sources.length) return field === "date";`
    expect(screen.getByText("Transaction type")).toBeInTheDocument();
    expect(screen.getByLabelText("Widget amount min")).toBeInTheDocument();
  });
});
