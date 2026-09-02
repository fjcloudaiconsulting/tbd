/**
 * TBD-383 — the KPI prior-period delta, fenced through the REAL render paths.
 *
 * ⚠ WHY THIS FILE EXISTS, AND WHY IT MAY NEVER INJECT A VALUE.
 *
 * Before TBD-383 the delta arrived as a `priorValue` prop that NO production
 * caller ever passed. `renderReportWidget` (`/reports/[id]`, and the dashboard
 * via `renderDashboardWidget`) did not pass it; `widgetKit.renderWidgetByType`
 * (`/reports/new`) did not pass it. The feature was structurally dead in
 * production for the life of the widget — and green in CI the whole time,
 * because the only two tests covering it mounted `<KPIWidget priorValue={100}>`
 * directly and injected the value the app never supplied.
 *
 * So every delta assertion in this file goes through a production dispatcher,
 * and BOTH of them: a fence that records coverage of one path is not a fence on
 * the other. If a future change reintroduces an injected-from-outside prior
 * value, these fences must be the thing that stops it — which they can only do
 * if nothing here ever hands a prior value to the widget.
 *
 * The sibling widget components are stubbed (KPIWidget is NOT) purely so the
 * dispatchers can be exercised without a live Nivo tree; the kpi arm under test
 * is the real component throughout.
 */
import { act, cleanup } from "@testing-library/react";

import { renderWithSWR, screen, waitFor } from "../../../utils/render-with-swr";

import { renderReportWidget } from "@/components/reports/renderReportWidget";
import { renderWidgetByType } from "@/components/reports/widgetKit";
import { runQuery } from "@/lib/reports/api";
import type {
  CanvasFilters,
  Filter,
  KPIWidget as KPIWidgetType,
  ReportsQuery,
  ReportsQueryResponse,
  WidgetFilters,
} from "@/lib/reports/types";
import { mockReportSources } from "../../../utils/mock-report-sources";

vi.mock("@/lib/api", () => ({
  apiFetch: (path: string) => mockReportSources()(path),
}));

vi.mock("@/lib/reports/api", () => ({
  runQuery: vi.fn(),
}));

// Sibling arms only — the kpi arm stays REAL. (Each factory is inlined:
// `vi.mock` is hoisted above module scope, so a shared helper would be a
// use-before-initialization error.)
vi.mock("@/components/reports/widgets/BarWidget", () => ({
  default: () => <div data-testid="bar-stub" />,
}));
vi.mock("@/components/reports/widgets/LineWidget", () => ({
  default: () => <div data-testid="line-stub" />,
}));
vi.mock("@/components/reports/widgets/AreaWidget", () => ({
  default: () => <div data-testid="area-stub" />,
}));
vi.mock("@/components/reports/widgets/PieWidget", () => ({
  default: () => <div data-testid="pie-stub" />,
}));
vi.mock("@/components/reports/widgets/SparklineWidget", () => ({
  default: () => <div data-testid="sparkline-stub" />,
}));
vi.mock("@/components/reports/widgets/TableWidget", () => ({
  default: () => <div data-testid="table-stub" />,
}));
vi.mock("@/components/reports/widgets/SankeyWidget", () => ({
  default: () => <div data-testid="sankey-stub" />,
}));

// ── Fixtures ─────────────────────────────────────────────────────────────────

const JANUARY = { start: "2026-01-01", end: "2026-01-31" };
/** December 2026-01-01..2026-01-31 shifted back one whole window. */
const PRIOR_DECEMBER: [string, string] = ["2025-12-01", "2025-12-31"];
/** 28 days; its prior 28 days are 2026-01-04..2026-01-31. */
const FEBRUARY = { start: "2026-02-01", end: "2026-02-28" };

const NO_CANVAS: CanvasFilters = {};

// ⚠ `"absent"`, not `undefined`: a default parameter cannot distinguish
// "caller omitted it" from "caller passed undefined", so `compare = true`
// would silently turn the absent-flag case (F11) into a duplicate of F1.
// ⚠ Deterministic, not random. A random id per call means no two renders in a
// test can ever be "the same widget", which silently makes any fence about
// SWR cache-key derivation unwritable (Hole 4): the key changes for the wrong
// reason and a mutant that drops the query from the key survives.
let widgetSeq = 0;

function makeKpi(
  filters: WidgetFilters | undefined,
  compare: boolean | "absent",
  id?: string,
): KPIWidgetType {
  return {
    id: id ?? `w_kpi_${++widgetSeq}`,
    type: "kpi",
    title: "Total spend",
    grid: { x: 0, y: 0, w: 3, h: 2 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      ...(filters ? { filters } : {}),
      ...(compare === "absent" ? {} : { compare_prior_period: compare }),
    },
  };
}

function dateFilterOf(q: ReportsQuery): Filter | undefined {
  return q.filters.find((f) => f.field === "date");
}

/** Window-start-keyed variant of `isPriorWindow`, for non-January fixtures. */
function isPriorWindowFor(start: string) {
  return (q: ReportsQuery): boolean => {
    const d = dateFilterOf(q);
    return !!d && d.op === "between" && Array.isArray(d.value) && d.value[0] === start;
  };
}

function isPriorWindow(q: ReportsQuery): boolean {
  const d = dateFilterOf(q);
  return (
    !!d &&
    d.op === "between" &&
    Array.isArray(d.value) &&
    d.value[0] === PRIOR_DECEMBER[0] &&
    d.value[1] === PRIOR_DECEMBER[1]
  );
}

const runQueryMock = vi.mocked(runQuery);

/**
 * Answer each query by the START of its date window.
 *
 * Throws on an unrecognised window, loudly and on purpose: a wrong comparison
 * window must fail as a wrong window, not decay into "no delta" where it is
 * indistinguishable from a deliberate refusal.
 */
function respondByWindowStart(values: Record<string, number>) {
  runQueryMock.mockImplementation(
    async (q: ReportsQuery): Promise<ReportsQueryResponse> => {
      const d = dateFilterOf(q);
      const key = Array.isArray(d?.value) ? String(d.value[0]) : "__unbounded__";
      const v = values[key];
      if (v === undefined) {
        throw new Error(`unexpected query window: ${JSON.stringify(d?.value)}`);
      }
      return {
        rows: [{ value: v }],
        meta: { row_count: 1, truncated: false, query_ms: 1 },
      };
    },
  );
}

/** Current window answers `current`; the shifted window answers `prior`. */
function respondWith(current: number, prior: number) {
  runQueryMock.mockImplementation(
    async (q: ReportsQuery): Promise<ReportsQueryResponse> => ({
      rows: [{ value: isPriorWindow(q) ? prior : current }],
      meta: { row_count: 1, truncated: false, query_ms: 1 },
    }),
  );
}

/**
 * Flush pending microtasks inside `act()`.
 *
 * A refused-comparison case fires one query; a live one fires two, and the
 * second can resolve after the last assertion. That state update would land
 * outside `act()` and the act-guard baseline is STRICT equality, so it would
 * fail the suite as a new warning. Settling inside `act()` keeps the count at
 * zero for this file.
 */
async function settle() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  runQueryMock.mockReset();
});

// ── F1/F2: the delta reaches the screen through BOTH production paths ────────

describe("TBD-383 F1/F2 — the delta renders through the production dispatchers", () => {
  it("F1: renderReportWidget renders the delta with no injected prior value", async () => {
    respondWith(200, 100);
    const w = makeKpi({ date_range: JANUARY }, true);

    renderWithSWR(<>{renderReportWidget(w, NO_CANVAS, false)}</>);

    const delta = await screen.findByTestId("kpi-widget-delta");
    expect(delta.textContent).toContain("+100.0%");
    expect(delta.textContent).toContain("vs prior period");
    await settle();
  });

  it("F2: widgetKit.renderWidgetByType renders the delta too", async () => {
    respondWith(200, 100);
    const w = makeKpi({ date_range: JANUARY }, true);

    renderWithSWR(<>{renderWidgetByType(w, NO_CANVAS, false)}</>);

    const delta = await screen.findByTestId("kpi-widget-delta");
    expect(delta.textContent).toContain("+100.0%");
    await settle();
  });
});

// ── F3: the SHIFTED window, not merely "a second query" ──────────────────────

describe("TBD-383 F3 — the comparison query carries the shifted window", () => {
  it("F3: the prior query is the window shifted back by its own length", async () => {
    respondWith(200, 100);
    const w = makeKpi({ date_range: JANUARY }, true);

    renderWithSWR(<>{renderReportWidget(w, NO_CANVAS, false)}</>);
    await screen.findByTestId("kpi-widget-delta");

    expect(runQueryMock).toHaveBeenCalledTimes(2);
    const windows = runQueryMock.mock.calls.map((c) => dateFilterOf(c[0])?.value);
    // Exact windows, both of them. Asserting only "two queries fired" would
    // pass against a comparison query that re-ran the SAME window.
    expect(windows).toContainEqual([JANUARY.start, JANUARY.end]);
    expect(windows).toContainEqual([PRIOR_DECEMBER[0], PRIOR_DECEMBER[1]]);
    await settle();
  });

  it("F3b: the comparison query differs from the current one ONLY in the date window", async () => {
    respondWith(200, 100);
    const w = makeKpi(
      {
        date_range: JANUARY,
        txn_type: ["expense"],
        status: "settled",
        // ⚠ `true`, not omitted. Left at its default both sides are `false`
        // and forcing the flag on the comparison query survives the compare
        // (Hole 2) — the field would be asserted vacuously.
        include_non_reportable: true,
      },
      true,
    );

    renderWithSWR(<>{renderReportWidget(w, NO_CANVAS, false)}</>);
    await screen.findByTestId("kpi-widget-delta");

    expect(runQueryMock).toHaveBeenCalledTimes(2);
    const calls = runQueryMock.mock.calls.map((c) => c[0]);
    // ⚠ The classifier must FAIL CLOSED. `isPriorWindow(a) ? a : b` silently
    // nominates `b` as "the prior query" when NEITHER call carries the prior
    // window, so the whole test stayed green under both the same-window and
    // the off-by-one mutants (Hole 6). Demand exactly one match first.
    const priorCalls = calls.filter(isPriorWindow);
    expect(priorCalls).toHaveLength(1);
    const prior = priorCalls[0];
    const current = calls.find((q) => q !== prior) as ReportsQuery;
    const strip = (q: ReportsQuery) => ({
      ...q,
      filters: q.filters.filter((f) => f.field !== "date"),
    });
    expect(strip(prior)).toEqual(strip(current));
    // ...and the date window is the one thing that DID change. Stripping it
    // before comparing means the compare above can never observe it.
    expect(dateFilterOf(prior)).not.toEqual(dateFilterOf(current));
    // ...and the non-date filters actually survived the shift, so the
    // comparison is over the same population.
    expect(prior.filters).toContainEqual({
      field: "txn_type",
      op: "in",
      value: ["expense"],
    });
    expect(prior.filters).toContainEqual({
      field: "status",
      op: "eq",
      value: "settled",
    });
    expect(prior.include_non_reportable).toBe(true);
    await settle();
  });
});

// ── F4-F7: every refusal renders the value alone, and for the RIGHT reason ───

describe("TBD-383 F4-F7 — refusals render the value with no delta", () => {
  it("F4: no date filter at all (unbounded) — value, no delta, one query", async () => {
    respondWith(200, 100);
    const w = makeKpi(undefined, true);

    renderWithSWR(<>{renderReportWidget(w, NO_CANVAS, false)}</>);
    await screen.findByTestId("kpi-widget-value");

    await waitFor(() => expect(runQueryMock).toHaveBeenCalledTimes(1));
    // ⚠ FIXTURE INTEGRITY, NOT A REFUSAL FENCE. This asserts the setup really
    // did produce an unbounded query — without it a typo in the fixture would
    // make the case vacuous. It does NOT discriminate between implementations:
    // F4 passes under the "no comparison wired at all" mutant, under the
    // read-start/end mutant, and under the invented-length mutant. F5/F6/F7
    // are the ones that discriminate.
    expect(dateFilterOf(runQueryMock.mock.calls[0][0])).toBeUndefined();
    expect(screen.queryByTestId("kpi-widget-delta")).toBeNull();
    await settle();
  });

  it("F5: a start-only range compiles to `gte` — half-open, no delta", async () => {
    respondWith(200, 100);
    const w = makeKpi({ date_range: { start: "2026-01-01" } }, true);

    renderWithSWR(<>{renderReportWidget(w, NO_CANVAS, false)}</>);
    await screen.findByTestId("kpi-widget-value");

    await waitFor(() => expect(runQueryMock).toHaveBeenCalledTimes(1));
    // Distinguishing: prove the refused filter was the half-open one.
    expect(dateFilterOf(runQueryMock.mock.calls[0][0])).toEqual({
      field: "date",
      op: "gte",
      value: "2026-01-01",
    });
    expect(screen.queryByTestId("kpi-widget-delta")).toBeNull();
    await settle();
  });

  it("F6: an end-only range compiles to `lte` — half-open, no delta", async () => {
    respondWith(200, 100);
    const w = makeKpi({ date_range: { end: "2026-01-31" } }, true);

    renderWithSWR(<>{renderReportWidget(w, NO_CANVAS, false)}</>);
    await screen.findByTestId("kpi-widget-value");

    await waitFor(() => expect(runQueryMock).toHaveBeenCalledTimes(1));
    expect(dateFilterOf(runQueryMock.mock.calls[0][0])).toEqual({
      field: "date",
      op: "lte",
      value: "2026-01-31",
    });
    expect(screen.queryByTestId("kpi-widget-delta")).toBeNull();
    await settle();
  });

  it("F7: a relative token (`next_cycle`) is refused DELIBERATELY, not by accident", async () => {
    respondWith(200, 100);
    const w = makeKpi({ date_range: { preset: "next_cycle" } }, true);

    renderWithSWR(<>{renderReportWidget(w, NO_CANVAS, false)}</>);
    await screen.findByTestId("kpi-widget-value");

    await waitFor(() => expect(runQueryMock).toHaveBeenCalledTimes(1));
    // ⚠ THE DISTINGUISHING ASSERTION. The client holds only a token here; the
    // absolute window is resolved server-side per request. An implementation
    // that reached for `dr.start` / `dr.end` would get `undefined` and refuse
    // too — right answer, wrong reason, and wrong the moment a token gains a
    // client-visible window. Asserting the query that DID fire carries
    // `op: "relative"` proves the relative branch was genuinely exercised;
    // the reason itself is fenced on the pure resolver in
    // `tests/lib/reports/prior-window.test.ts` (`relative_token`).
    expect(dateFilterOf(runQueryMock.mock.calls[0][0])).toEqual({
      field: "date",
      op: "relative",
      value: "next_cycle",
    });
    expect(screen.queryByTestId("kpi-widget-delta")).toBeNull();
    await settle();
  });
});

// ── F8/F9: the delta is never coloured by sign ───────────────────────────────

describe("TBD-383 F8/F9 — the delta carries no judgement colour", () => {
  // ⚠ These deliberately do NOT assert the literal `text-text-secondary`, nor
  // the ↑/↓ glyphs. Re-tokenising the neutral colour, or replacing the glyph
  // with an `<svg>` (an accessibility improvement), is not a regression and
  // must not turn this file red. Direction is asserted through the sign in the
  // text, which is also the only part a screen reader gets — the glyph is
  // `aria-hidden`.
  //
  // ⚠⚠ ALL THREE ARE LOAD-BEARING. F8b is NOT a stronger replacement for
  // F8/F9 — do not delete either as redundant. Measured, five mutants:
  //
  //   mutant                                     F8     F9     F8b
  //   success-if-up / danger-if-down             RED    RED    RED
  //   neutral-if-up / danger-if-down             green  RED    RED
  //   ALWAYS text-success                        RED    RED    green
  //   ALWAYS text-danger                         RED    RED    green
  //   two NEUTRAL tokens, varying by sign        green  green  RED
  //
  // The two halves are blind to different things. F8b compares the class
  // across signs, so a CONSTANT judgement colour is invisible to it — the two
  // classes are equal and it passes. F8/F9 name the two judgement tokens, so
  // styling that varies by sign WITHOUT using them is invisible to them. No
  // pair covers the space.
  async function renderDelta(current: number, prior: number) {
    respondWith(current, prior);
    const w = makeKpi({ date_range: JANUARY }, true);
    renderWithSWR(<>{renderReportWidget(w, NO_CANVAS, false)}</>);
    return screen.findByTestId("kpi-widget-delta");
  }

  it("F8: a POSITIVE delta is not painted with the success token", async () => {
    const delta = await renderDelta(200, 100);

    expect(delta.textContent).toContain("+100.0%");
    expect(delta.className).not.toContain("text-success");
    expect(delta.className).not.toContain("text-danger");
    await settle();
  });

  it("F9: a NEGATIVE delta is not painted with the danger token", async () => {
    const delta = await renderDelta(50, 100);

    expect(delta.textContent).toContain("-50.0%");
    expect(delta.className).not.toContain("text-danger");
    expect(delta.className).not.toContain("text-success");
    await settle();
  });

  it("F8b: the delta's styling does not VARY with the sign", async () => {
    // The invariant, stated directly. A one-directional fence passes against
    // "neutral when positive, red when negative"; this does not, and it
    // survives any future re-tokenising of the neutral colour.
    const up = await renderDelta(200, 100);
    const upClass = up.className;
    await settle();
    cleanup();

    const down = await renderDelta(50, 100);
    expect(down.className).toBe(upClass);
    await settle();
  });
});

// ── F10/F11: the 95% path gains no network call ──────────────────────────────

describe("TBD-383 F10/F11 — comparison off fires no second query", () => {
  it("F10: compare_prior_period: false fires exactly one query", async () => {
    respondWith(200, 100);
    const w = makeKpi({ date_range: JANUARY }, false);

    renderWithSWR(<>{renderReportWidget(w, NO_CANVAS, false)}</>);
    await screen.findByTestId("kpi-widget-value");

    await waitFor(() => expect(runQueryMock).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId("kpi-widget-delta")).toBeNull();
    await settle();
    expect(runQueryMock).toHaveBeenCalledTimes(1);
  });

  it("F11: an ABSENT compare_prior_period fires exactly one query", async () => {
    respondWith(200, 100);
    const w = makeKpi({ date_range: JANUARY }, "absent");

    renderWithSWR(<>{renderReportWidget(w, NO_CANVAS, false)}</>);
    await screen.findByTestId("kpi-widget-value");

    await waitFor(() => expect(runQueryMock).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId("kpi-widget-delta")).toBeNull();
    await settle();
    expect(runQueryMock).toHaveBeenCalledTimes(1);
  });
});

// ── F12: division guard ──────────────────────────────────────────────────────

describe("TBD-383 F12 — a prior value of zero yields no delta", () => {
  it("F12: prior 0 renders the value with no delta (no division by zero)", async () => {
    respondWith(200, 0);
    const w = makeKpi({ date_range: JANUARY }, true);

    renderWithSWR(<>{renderReportWidget(w, NO_CANVAS, false)}</>);
    await screen.findByTestId("kpi-widget-value");

    // The comparison DID run — this is the guard, not a refusal.
    await waitFor(() => expect(runQueryMock).toHaveBeenCalledTimes(2));
    await settle();
    expect(screen.queryByTestId("kpi-widget-delta")).toBeNull();
  });
});

// ── B1: a window that has not finished yet compares like for like ────────────

describe("TBD-383 B1 — the seeded `this_month` preset is clamped to the days it has reached", () => {
  // ⚠⚠ THE DEFECT THIS EXISTS FOR. `draft.ts` seeds every new report with
  // `this_month`, frozen by `buildPresetRanges` as the WHOLE calendar month.
  // Unclamped, on 2026-09-02 the widget compared 2 days of September against
  // 30 complete days of August and rendered about "-92% vs prior period" for a
  // user whose spending had not changed. Only `this_month` breaks — which is
  // exactly why it survived review: `last_month` is exactly right and
  // `ytd`/`last_12_months` end at `now`, so testing on any other preset looks
  // perfect.
  //
  // ⚠ The clamp is NOT complete-to-complete: `today` counts as a whole day on
  // both sides, so unchanged spending still reads about -29% on day 2 at 10:00.
  // These fixtures pin the WINDOW, which is exact; the residual partial-day
  // skew is described in `resolvePriorWindow`'s docstring.
  //
  // Only `Date` is faked, so Testing Library's real timers still drive
  // `findBy*`/`waitFor`. A fixed clock also means this is not a date bomb.
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    // Local components on purpose: `todayIso()` reads the local clock,
    // matching how `date-presets.ts` builds the windows it is compared with.
    vi.setSystemTime(new Date(2026, 8, 2, 10, 0, 0));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  const SEPTEMBER = { start: "2026-09-01", end: "2026-09-30" };

  it("B1: the prior window is the 2 ELAPSED days, not the whole prior month", async () => {
    respondByWindowStart({
      "2026-09-01": 250, // September so far (2 days)
      "2026-08-30": 500, // the matching 2 days, Aug 30-31
    });
    const w = makeKpi({ date_range: SEPTEMBER }, true);

    renderWithSWR(<>{renderReportWidget(w, NO_CANVAS, false)}</>);
    const delta = await screen.findByTestId("kpi-widget-delta");

    // The exact window is the assertion. An unclamped implementation asks for
    // 2026-08-02..08-31 and the responder throws on it, so this cannot pass by
    // decaying into "no delta".
    const prior = runQueryMock.mock.calls.map((c) => c[0]).filter(isPriorWindowFor("2026-08-30"));
    expect(prior).toHaveLength(1);
    expect(dateFilterOf(prior[0])).toEqual({
      field: "date",
      op: "between",
      value: ["2026-08-30", "2026-08-31"],
    });
    expect(delta.textContent).toContain("-50.0%");
    await settle();
  });

  it("B1b: on the FIRST of the month it is one day against one day", async () => {
    vi.setSystemTime(new Date(2026, 8, 1, 9, 0, 0));
    respondByWindowStart({
      "2026-09-01": 100, // day one
      "2026-08-31": 100, // the day before
    });
    const w = makeKpi({ date_range: SEPTEMBER }, true);

    renderWithSWR(<>{renderReportWidget(w, NO_CANVAS, false)}</>);
    const delta = await screen.findByTestId("kpi-widget-delta");

    // Unchanged spending reads as unchanged. Unclamped this was ~-100%.
    // No sign and no arrow: see the rounds-to-zero block below.
    expect(delta.textContent).toContain("0.0%");
    expect(delta.textContent).not.toContain("↑");
    await settle();
  });

  it("B1c: a FULLY PAST window is untouched by the clamp", async () => {
    respondByWindowStart({ "2026-01-01": 200, "2025-12-01": 100 });
    const w = makeKpi({ date_range: JANUARY }, true);

    renderWithSWR(<>{renderReportWidget(w, NO_CANVAS, false)}</>);
    const delta = await screen.findByTestId("kpi-widget-delta");

    expect(delta.textContent).toContain("+100.0%");
    await settle();
  });
});

// ── Hole 1: the canvas cascade, the commonest path of all ────────────────────

describe("TBD-383 Hole 1 — a window inherited from the CANVAS drives the comparison", () => {
  // ⚠⚠ Every other fixture in this file puts the date range on the WIDGET, so
  // building the comparison AST with `canvasFilters` dropped survived all of
  // them. On a report canvas the shared canvas date is the primary way a
  // window reaches a KPI (and the dashboard passes one too): under that
  // mutant a canvas-filtered KPI resolves to `no_date_filter` and shows no
  // delta at all — the exact silent deadness this ticket exists to kill,
  // reintroduced on the commonest path.
  it("Hole 1: widget has NO date range; the canvas supplies January", async () => {
    respondByWindowStart({ "2026-01-01": 200, "2025-12-01": 100 });
    const w = makeKpi(undefined, true);
    const canvas: CanvasFilters = { date_range: JANUARY };

    renderWithSWR(<>{renderReportWidget(w, canvas, false)}</>);
    const delta = await screen.findByTestId("kpi-widget-delta");

    expect(delta.textContent).toContain("+100.0%");
    expect(runQueryMock).toHaveBeenCalledTimes(2);
    const prior = runQueryMock.mock.calls.map((c) => c[0]).filter(isPriorWindow);
    expect(prior).toHaveLength(1);
    expect(dateFilterOf(prior[0])).toEqual({
      field: "date",
      op: "between",
      value: [PRIOR_DECEMBER[0], PRIOR_DECEMBER[1]],
    });
    await settle();
  });

  it("Hole 1b: the same widget follows the canvas when the canvas window CHANGES", async () => {
    respondByWindowStart({
      "2026-01-01": 200,
      "2025-12-01": 100, // prior of January
      "2026-02-01": 300,
      "2026-01-04": 250, // prior of February (28 elapsed days back)
    });
    const w = makeKpi(undefined, true, "w_canvas_follow");

    const { rerender } = renderWithSWR(
      <>{renderReportWidget(w, { date_range: JANUARY }, false)}</>,
    );
    expect((await screen.findByTestId("kpi-widget-delta")).textContent).toContain(
      "+100.0%",
    );

    rerender(
      <>{renderReportWidget(w, { date_range: FEBRUARY }, false)}</>,
    );
    await waitFor(() =>
      expect(screen.getByTestId("kpi-widget-delta").textContent).toContain(
        "+20.0%",
      ),
    );
    await settle();
  });
});

// ── Hole 4: the prior SWR key must carry the query, not just the widget id ───

describe("TBD-383 Hole 4 — the comparison cache key includes the query", () => {
  it("Hole 4: editing the window does not leave the OLD prior under the new headline", async () => {
    // ⚠ Same `widget.id` across both renders, on purpose. If the prior SWR key
    // is only `["report-query-prior", widget.id]`, the primary query (whose
    // key DOES carry its AST) moves to February while the comparison keeps
    // January's cached prior — a delta computed from two different windows,
    // rendered with no sign that anything is stale.
    //
    // The three deltas are deliberately distinct: January +100.0%, correct
    // February +20.0%, stale-prior February +200.0%.
    respondByWindowStart({
      "2026-01-01": 200,
      "2025-12-01": 100,
      "2026-02-01": 300,
      "2026-01-04": 250,
    });
    const jan = makeKpi({ date_range: JANUARY }, true, "w_key_fence");
    const feb = makeKpi({ date_range: FEBRUARY }, true, "w_key_fence");

    const { rerender } = renderWithSWR(
      <>{renderReportWidget(jan, NO_CANVAS, false)}</>,
    );
    expect((await screen.findByTestId("kpi-widget-delta")).textContent).toContain(
      "+100.0%",
    );

    rerender(<>{renderReportWidget(feb, NO_CANVAS, false)}</>);
    await waitFor(() =>
      expect(screen.getByTestId("kpi-widget-delta").textContent).toContain(
        "+20.0%",
      ),
    );
    expect(screen.getByTestId("kpi-widget-delta").textContent).not.toContain(
      "+200.0%",
    );
    await settle();
  });
});

// ── The glyph must agree with the number it labels ───────────────────────────

describe("TBD-383 — a delta that rounds to zero carries no direction", () => {
  // ⚠ The fold's own arrow asserted a false fact. `delta >= 0 ? "↑" : "↓"`
  // rendered "↑ +0.0% vs prior period" for a KPI whose spend did not change —
  // the exact case B1b celebrates — and "↓ -0.0%" for a delta of -0.04.
  //
  // ⚠ The direction is derived from the ROUNDED value, not the raw one, so the
  // glyph can never disagree with the digits printed beside it. Deriving it
  // from the raw delta passes the exact-zero case and still renders "↑ 0.0%"
  // at +0.02 — right answer, wrong quantity.
  it("an UNCHANGED value renders no up arrow and no plus sign", async () => {
    respondByWindowStart({ "2026-01-01": 100, "2025-12-01": 100 });
    const w = makeKpi({ date_range: JANUARY }, true);

    renderWithSWR(<>{renderReportWidget(w, NO_CANVAS, false)}</>);
    const delta = await screen.findByTestId("kpi-widget-delta");

    expect(delta.textContent).toContain("0.0%");
    expect(delta.textContent).toContain("→");
    expect(delta.textContent).not.toContain("↑");
    expect(delta.textContent).not.toContain("+");
    await settle();
  });

  it("a POSITIVE delta that rounds to 0.0 renders no up arrow", async () => {
    // +0.02% — below the 0.05 that would round to 0.1.
    respondByWindowStart({ "2026-01-01": 10002, "2025-12-01": 10000 });
    const w = makeKpi({ date_range: JANUARY }, true);

    renderWithSWR(<>{renderReportWidget(w, NO_CANVAS, false)}</>);
    const delta = await screen.findByTestId("kpi-widget-delta");

    expect(delta.textContent).toContain("0.0%");
    expect(delta.textContent).not.toContain("↑");
    expect(delta.textContent).not.toContain("+");
    await settle();
  });

  it("a NEGATIVE delta that rounds to 0.0 renders no down arrow and no '-0.0'", async () => {
    // -0.02%. `(-0.02).toFixed(1)` is the string "-0.0", which is its own
    // small lie: a minus sign on a quantity of zero.
    respondByWindowStart({ "2026-01-01": 9998, "2025-12-01": 10000 });
    const w = makeKpi({ date_range: JANUARY }, true);

    renderWithSWR(<>{renderReportWidget(w, NO_CANVAS, false)}</>);
    const delta = await screen.findByTestId("kpi-widget-delta");

    expect(delta.textContent).toContain("0.0%");
    expect(delta.textContent).not.toContain("↓");
    expect(delta.textContent).not.toContain("-0.0");
    await settle();
  });

  it("a real movement still carries its arrow and sign in both directions", async () => {
    // Guards the fix from over-reaching into "never show a direction".
    respondByWindowStart({ "2026-01-01": 200, "2025-12-01": 100 });
    const up = makeKpi({ date_range: JANUARY }, true);
    renderWithSWR(<>{renderReportWidget(up, NO_CANVAS, false)}</>);
    const upDelta = await screen.findByTestId("kpi-widget-delta");
    expect(upDelta.textContent).toContain("↑");
    expect(upDelta.textContent).toContain("+100.0%");
    await settle();
    cleanup();

    respondByWindowStart({ "2026-01-01": 50, "2025-12-01": 100 });
    const down = makeKpi({ date_range: JANUARY }, true);
    renderWithSWR(<>{renderReportWidget(down, NO_CANVAS, false)}</>);
    const downDelta = await screen.findByTestId("kpi-widget-delta");
    expect(downDelta.textContent).toContain("↓");
    expect(downDelta.textContent).toContain("-50.0%");
    await settle();
  });
});
