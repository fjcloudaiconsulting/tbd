/**
 * TBD-486 — `line` / `area` REFUSE a second dimension; they never draw one.
 *
 * The defect these fences pin: `mergeSeriesRows` keys on the PRIMARY
 * dimension and does `existing[key] = readNumber(row.value)` — an
 * ASSIGNMENT. With `dimensions: ["month", "category"]` the rows
 * `(2026-01, Rent, 900)` and `(2026-01, Groceries, 100)` both key to
 * `"2026-01"`, so the second overwrote the first and the chart plotted one
 * arbitrary category's figure as January's — the axis, the tooltip and the
 * CSV all presenting it as the month's. Same shape TBD-382 fixed for
 * `stacked_bar`.
 *
 * ⚠ WHICH pair wins is arbitrary, so no fence here may name an "expected
 * wrong value". With no `sort` the compiler orders `value DESC`; the seeded
 * line / area widgets carry `sort: {by:"dimension", dir:"asc"}` over `month`,
 * which does not order WITHIN a month at all. F1/F6 therefore forbid every
 * value the fixture can surface, not one of them.
 *
 * ⚠ Every fence here MOUNTS the real widget with real rows. A unit test of
 * `hasSecondDimension` would be vacuous: the predicate is trivially correct
 * and the shipped defect was that nothing consulted one.
 *
 * ⚠ The chart module is MOCKED, and that is what makes the wrong NUMBER
 * observable. jsdom collapses the recharts subtree to 0×0, so a fence that
 * only looked at the real chart could not tell "plotted 100" from "plotted
 * nothing" — and would stay green against the shipped defect. The stub
 * renders each point's value as text, so `textContent` is the surface.
 *
 * ⚠ The copy is asserted as a LITERAL substring written out below, never as
 * the imported constant. `toHaveTextContent(SECOND_DIMENSION_UNSUPPORTED_NOTICE)`
 * is self-referential: it proves the constant reaches the DOM and stays green
 * when the constant is `""`, i.e. against an empty grey box where the chart
 * used to be. The message IS the deliverable.
 *
 * ⚠ The refusal PRESERVES the config. Same ruling as `UNSUPPORTED_MEASURE_KEY`
 * (`controlConstants.ts`): "Rewriting would change the number a saved report
 * renders without telling anyone." F3/F8 snapshot the config across the render
 * because a version that shows the message AND silently drops `dimensions[1]`
 * passes every other fence in this file. Building the break-down is TBD-383.
 */
import {
  renderWithSWR,
  screen,
  within,
} from "../../../utils/render-with-swr";

import LineWidget from "@/components/reports/widgets/LineWidget";
import AreaWidget from "@/components/reports/widgets/AreaWidget";
import type {
  AreaWidget as AreaWidgetType,
  LineWidget as LineWidgetType,
  ReportsQuery,
} from "@/lib/reports/types";
import { runQuery } from "@/lib/reports/api";
import { mockReportSources } from "../../../utils/mock-report-sources";

vi.mock("@/lib/api", () => ({
  apiFetch: (path: string) => mockReportSources()(path),
}));

vi.mock("@/lib/reports/api", () => ({
  runQuery: vi.fn(),
}));

/**
 * A literal fragment of the shipped sentence. Deliberately NOT the imported
 * constant — see the ⚠ in the module docstring. Kept to the clause that
 * carries the meaning, so ordinary copy-editing elsewhere in the sentence
 * does not produce a false red.
 */
const NOTICE_FRAGMENT = "grouped by more than one dimension";

/** See the ⚠ in the module docstring: the stub is what makes a value visible. */
function chartStub(testid: string) {
  function ChartStub(props: {
    rows: Array<Record<string, number | string>>;
    seriesKeys: string[];
  }) {
    return (
      <div data-testid={testid} data-rows={JSON.stringify(props.rows)}>
        {props.rows.map((r) => (
          <span key={String(r.label)} data-testid={`${testid}-point-${r.label}`}>
            {props.seriesKeys.map((k) => String(r[k])).join(",")}
          </span>
        ))}
      </div>
    );
  }
  return ChartStub;
}

vi.mock("@/components/reports/widgets/LineWidgetChart", () => ({
  default: chartStub("line-chart-stub"),
}));

vi.mock("@/components/reports/widgets/AreaWidgetChart", () => ({
  default: chartStub("area-chart-stub"),
}));

/**
 * Two categories inside ONE month, delivered in the compiler's default
 * `ORDER BY value DESC`. Under last-write-wins the merge renders 100 for
 * 2026-01 and 40 for 2026-02 — one arbitrary category's number labelled as
 * the whole month's.
 */
const TWO_DIMENSION_ROWS = {
  rows: [
    { month: "2026-01", category: "Rent", value: 900 },
    { month: "2026-01", category: "Groceries", value: 100 },
    { month: "2026-02", category: "Rent", value: 800 },
    { month: "2026-02", category: "Coffee", value: 40 },
  ],
  meta: { row_count: 4, truncated: false, query_ms: 3 },
};

/**
 * Every value the fixture above can surface as a data point under the defect,
 * as whole tokens. ⚠ Whole tokens, not bare substrings: `not.toContain("40")`
 * also fires on "404" or any amount ending in 40, which would be a
 * misleading red for a completely unrelated change.
 */
const WRONG_VALUES = /\b(100|40)\b/;

/** What the SENTINEL (one dimension) returns. Deliberately disjoint from the
 *  values above so a leak across the two cards is unmistakable. */
const ONE_DIMENSION_ROWS = {
  rows: [
    { month: "2026-01", value: 1000 },
    { month: "2026-02", value: 840 },
  ],
  meta: { row_count: 2, truncated: false, query_ms: 3 },
};

const SUBJECT_ID = "w_subject";
const SENTINEL_ID = "w_sentinel";

function makeLine(
  dimensions: LineWidgetType["config"]["dimensions"],
  id = SUBJECT_ID,
): LineWidgetType {
  return {
    id,
    type: "line",
    title: "Net by month",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measures: [{ measure: { agg: "sum", field: "amount" } }],
      dimensions,
      sort: { by: "dimension", dir: "asc" },
      limit: 12,
    },
  };
}

function makeArea(
  dimensions: AreaWidgetType["config"]["dimensions"],
  id = SUBJECT_ID,
): AreaWidgetType {
  return {
    id,
    type: "area",
    title: "Net by month",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measures: [{ measure: { agg: "sum", field: "amount" } }],
      dimensions,
      sort: { by: "dimension", dir: "asc" },
      limit: 12,
    },
  };
}

/**
 * Route the mock by DIMENSION COUNT, so one `runQuery` mock serves the
 * multi-dimension subject and the one-dimension sentinel differently in the
 * same test — including the fences where the subject's query must FAIL while
 * the sentinel's must succeed.
 */
function respond(subject: () => unknown) {
  vi.mocked(runQuery).mockImplementation(async (q: ReportsQuery) => {
    if ((q.dimensions ?? []).length > 1) return subject() as never;
    return ONE_DIMENSION_ROWS as never;
  });
}

/**
 * R1 — how these fences reach a SETTLED DOM.
 *
 * The refusal renders on the FIRST paint, so `findByTestId` on it resolves
 * before any query has come back, and every "the chart is absent" assertion
 * after it would pass vacuously. An earlier revision awaited `runQuery`
 * having been CALLED, which (a) proved only that a fetch STARTED and (b)
 * hard-coded "a refusing widget must still fetch" into eight fences — so a
 * defensible optimisation would have turned them all red with a message
 * about a mock call count that says nothing about intent.
 *
 * So each fence mounts a one-dimension SENTINEL beside the subject. The
 * sentinel's chart appears only after a query RESOLVED and React re-rendered,
 * so awaiting it proves the full round trip completed in this environment,
 * while asserting nothing about whether the SUBJECT fetched.
 *
 * ⚠ Both cards carry the same `data-testid`, so every subject assertion is
 * scoped through `within(...)` on the card matched by `data-widget-id`.
 */
function cardFor(tid: string, widgetId: string): HTMLElement {
  const card = screen
    .getAllByTestId(tid)
    .find((c) => c.getAttribute("data-widget-id") === widgetId);
  if (!card) throw new Error(`no ${tid} carrying data-widget-id=${widgetId}`);
  return card;
}

function mountLine(subject: LineWidgetType): HTMLElement {
  renderWithSWR(
    <>
      <LineWidget widget={subject} />
      <LineWidget widget={makeLine(["month"], SENTINEL_ID)} />
    </>,
  );
  return cardFor("line-widget", subject.id);
}

function mountArea(subject: AreaWidgetType): HTMLElement {
  renderWithSWR(
    <>
      <AreaWidget widget={subject} />
      <AreaWidget widget={makeArea(["month"], SENTINEL_ID)} />
    </>,
  );
  return cardFor("area-widget", subject.id);
}

/**
 * Await the SENTINEL's chart. Split out from `mount*` because the first-paint
 * fences (F15/F16) must assert BEFORE it and settle AFTER: an unsettled
 * SWR resolution escapes `act()`, and this suite's act-warning baseline is
 * a strict-equality gate (TBD-393), so leaving one is a build failure and
 * not merely untidy.
 */
async function settleLine(): Promise<void> {
  await within(cardFor("line-widget", SENTINEL_ID)).findByTestId(
    "line-chart-stub",
  );
}

async function settleArea(): Promise<void> {
  await within(cardFor("area-widget", SENTINEL_ID)).findByTestId(
    "area-chart-stub",
  );
}

async function renderLine(subject: LineWidgetType): Promise<HTMLElement> {
  const card = mountLine(subject);
  await settleLine();
  return card;
}

async function renderArea(subject: AreaWidgetType): Promise<HTMLElement> {
  const card = mountArea(subject);
  await settleArea();
  return card;
}

describe("line / area refuse a second dimension (TBD-486)", () => {
  beforeEach(() => {
    vi.mocked(runQuery).mockReset();
  });

  // ── F1 ────────────────────────────────────────────────────────────────
  it("F1: line does NOT plot the last returned pair's value as the month's value", async () => {
    respond(() => TWO_DIMENSION_ROWS);

    const card = await renderLine(makeLine(["month", "category"]));

    // The NUMBER. Under last-write-wins this card renders 100 for January
    // and 40 for February; both are one category's figure wearing the
    // month's label, and neither may appear.
    expect(card.textContent).not.toMatch(WRONG_VALUES);
    // And no point was drawn at all.
    expect(within(card).queryByTestId("line-chart-stub")).toBeNull();
    expect(
      within(card).queryByTestId("line-chart-stub-point-2026-01"),
    ).toBeNull();
    expect(
      within(card).getByTestId("line-widget-unsupported"),
    ).toBeInTheDocument();
  });

  // ── F2 ────────────────────────────────────────────────────────────────
  it("F2: line shows the unsupported COPY and NOT the chart", async () => {
    respond(() => TWO_DIMENSION_ROWS);

    const card = await renderLine(makeLine(["month", "category"]));

    const notice = within(card).getByTestId("line-widget-unsupported");
    // ⚠ A literal, not the imported constant. See the module docstring.
    expect(notice).toHaveTextContent(NOTICE_FRAGMENT);
    // NB-4: this is the first branch whose copy can exceed the tile, so it
    // scrolls — and WCAG 2.1.1 makes a scrollable region keyboard reachable.
    // Same treatment, and the same reason, as BarWidget's legend.
    expect(notice).toHaveAttribute("tabindex", "0");
    expect(within(card).queryByTestId("line-chart-stub")).toBeNull();
    expect(within(card).queryByTestId("line-widget-chart-loading")).toBeNull();
    expect(within(card).queryByTestId("line-widget-empty")).toBeNull();
  });

  // ── F3 ────────────────────────────────────────────────────────────────
  it("F3: line PRESERVES config.dimensions — the refusal never repairs it", async () => {
    respond(() => TWO_DIMENSION_ROWS);
    const widget = makeLine(["month", "category"]);
    const before = JSON.stringify(widget.config);

    await renderLine(widget);

    expect(widget.config.dimensions).toEqual(["month", "category"]);
    // Whole-config snapshot: a "repair" anywhere in the object is caught,
    // not just one that touches dimensions.
    expect(JSON.stringify(widget.config)).toBe(before);
  });

  // ── F4 ────────────────────────────────────────────────────────────────
  it("F4: line withholds the CSV export — the merged rows are the same wrong number", async () => {
    respond(() => TWO_DIMENSION_ROWS);

    const card = await renderLine(makeLine(["month", "category"]));

    expect(within(card).queryByTestId("widget-csv-export")).toBeNull();
    // The sentinel still offers one, so this is not "the button is gone
    // everywhere".
    expect(
      within(cardFor("line-widget", SENTINEL_ID)).getByTestId(
        "widget-csv-export",
      ),
    ).toBeInTheDocument();
  });

  // ── F5 — the regression net ───────────────────────────────────────────
  it("F5: line with ONE dimension still draws its chart", async () => {
    respond(() => TWO_DIMENSION_ROWS);

    renderWithSWR(<LineWidget widget={makeLine(["month"])} />);

    const stub = await screen.findByTestId("line-chart-stub");
    expect(JSON.parse(stub.getAttribute("data-rows")!)).toEqual([
      { label: "2026-01", s0: 1000 },
      { label: "2026-02", s0: 840 },
    ]);
    expect(screen.queryByTestId("line-widget-unsupported")).toBeNull();
    expect(screen.getByTestId("widget-csv-export")).toBeInTheDocument();
  });

  // ── F6 ────────────────────────────────────────────────────────────────
  it("F6: area does NOT plot the last returned pair's value as the month's value", async () => {
    respond(() => TWO_DIMENSION_ROWS);

    const card = await renderArea(makeArea(["month", "category"]));

    expect(card.textContent).not.toMatch(WRONG_VALUES);
    expect(within(card).queryByTestId("area-chart-stub")).toBeNull();
    expect(
      within(card).queryByTestId("area-chart-stub-point-2026-01"),
    ).toBeNull();
    expect(
      within(card).getByTestId("area-widget-unsupported"),
    ).toBeInTheDocument();
  });

  // ── F7 ────────────────────────────────────────────────────────────────
  it("F7: area shows the unsupported COPY and NOT the chart", async () => {
    respond(() => TWO_DIMENSION_ROWS);

    const card = await renderArea(makeArea(["month", "category"]));

    const notice = within(card).getByTestId("area-widget-unsupported");
    expect(notice).toHaveTextContent(NOTICE_FRAGMENT);
    expect(notice).toHaveAttribute("tabindex", "0");
    expect(within(card).queryByTestId("area-chart-stub")).toBeNull();
    expect(within(card).queryByTestId("area-widget-chart-loading")).toBeNull();
    expect(within(card).queryByTestId("area-widget-empty")).toBeNull();
  });

  // ── F8 ────────────────────────────────────────────────────────────────
  it("F8: area PRESERVES config.dimensions — the refusal never repairs it", async () => {
    respond(() => TWO_DIMENSION_ROWS);
    const widget = makeArea(["month", "category"]);
    const before = JSON.stringify(widget.config);

    await renderArea(widget);

    expect(widget.config.dimensions).toEqual(["month", "category"]);
    expect(JSON.stringify(widget.config)).toBe(before);
  });

  // ── F9 ────────────────────────────────────────────────────────────────
  it("F9: area withholds the CSV export", async () => {
    respond(() => TWO_DIMENSION_ROWS);

    const card = await renderArea(makeArea(["month", "category"]));

    expect(within(card).queryByTestId("widget-csv-export")).toBeNull();
    expect(
      within(cardFor("area-widget", SENTINEL_ID)).getByTestId(
        "widget-csv-export",
      ),
    ).toBeInTheDocument();
  });

  // ── F10 — the regression net ──────────────────────────────────────────
  it("F10: area with ONE dimension still draws its chart", async () => {
    respond(() => TWO_DIMENSION_ROWS);

    renderWithSWR(<AreaWidget widget={makeArea(["month"])} />);

    const stub = await screen.findByTestId("area-chart-stub");
    expect(JSON.parse(stub.getAttribute("data-rows")!)).toEqual([
      { label: "2026-01", s0: 1000 },
      { label: "2026-02", s0: 840 },
    ]);
    expect(screen.queryByTestId("area-widget-unsupported")).toBeNull();
    expect(screen.getByTestId("widget-csv-export")).toBeInTheDocument();
  });

  // ── F11 / F12 — the refusal OUTRANKS the error branch ─────────────────
  //
  // ⚠ Branch ORDER, and it is not cosmetic. Moved below `error`, a
  // two-dimension widget whose query fails shows "Couldn't load" FOREVER and
  // the user never learns the real reason. That is the realistic path, not a
  // hypothetical: a THREE-dimension layout is accepted by the layout schema
  // and 422s at query time (see F17/F18), so it lands in exactly this branch.

  it("F11: line shows the refusal, not the error state, when the query FAILS", async () => {
    respond(() => {
      throw new Error("boom");
    });

    const card = await renderLine(makeLine(["month", "category"]));

    expect(
      within(card).getByTestId("line-widget-unsupported"),
    ).toHaveTextContent(NOTICE_FRAGMENT);
    expect(within(card).queryByTestId("line-widget-error")).toBeNull();
  });

  it("F12: area shows the refusal, not the error state, when the query FAILS", async () => {
    respond(() => {
      throw new Error("boom");
    });

    const card = await renderArea(makeArea(["month", "category"]));

    expect(
      within(card).getByTestId("area-widget-unsupported"),
    ).toHaveTextContent(NOTICE_FRAGMENT);
    expect(within(card).queryByTestId("area-widget-error")).toBeNull();
  });

  // ── F13 / F14 — the refusal OUTRANKS the empty branch ─────────────────

  it("F13: line shows the refusal, not 'No data', when the query returns NO rows", async () => {
    respond(() => ({
      rows: [],
      meta: { row_count: 0, truncated: false, query_ms: 1 },
    }));

    const card = await renderLine(makeLine(["month", "category"]));

    expect(
      within(card).getByTestId("line-widget-unsupported"),
    ).toHaveTextContent(NOTICE_FRAGMENT);
    expect(within(card).queryByTestId("line-widget-empty")).toBeNull();
  });

  it("F14: area shows the refusal, not 'No data', when the query returns NO rows", async () => {
    respond(() => ({
      rows: [],
      meta: { row_count: 0, truncated: false, query_ms: 1 },
    }));

    const card = await renderArea(makeArea(["month", "category"]));

    expect(
      within(card).getByTestId("area-widget-unsupported"),
    ).toHaveTextContent(NOTICE_FRAGMENT);
    expect(within(card).queryByTestId("area-widget-empty")).toBeNull();
  });

  // ── F15 / F16 — the refusal OUTRANKS the loading branch ───────────────
  //
  // ⚠ Deliberately SYNCHRONOUS, with no sentinel and no await: the whole
  // claim is about the FIRST paint. Moved below `isLoading`, the widget shows
  // a skeleton that then flips into a refusal — a frame that says "fetching"
  // about a config no fetch can rescue. Awaiting anything here would let the
  // query settle and hide exactly that.

  it("F15: line refuses on the FIRST paint, never a skeleton first", async () => {
    respond(() => TWO_DIMENSION_ROWS);

    const card = mountLine(makeLine(["month", "category"]));

    // ⚠ Asserted BEFORE any await. The whole claim is about the first frame.
    expect(
      within(card).getByTestId("line-widget-unsupported"),
    ).toHaveTextContent(NOTICE_FRAGMENT);
    expect(within(card).queryByTestId("line-widget-loading")).toBeNull();

    // Settle only afterwards, so the in-flight resolution does not escape
    // act(). The assertions above have already run on the first frame.
    await settleLine();
  });

  it("F16: area refuses on the FIRST paint, never a skeleton first", async () => {
    respond(() => TWO_DIMENSION_ROWS);

    const card = mountArea(makeArea(["month", "category"]));

    expect(
      within(card).getByTestId("area-widget-unsupported"),
    ).toHaveTextContent(NOTICE_FRAGMENT);
    expect(within(card).queryByTestId("area-widget-loading")).toBeNull();

    await settleArea();
  });

  // ── F17 / F18 — THREE dimensions refuse too ───────────────────────────
  //
  // ⚠ `> 1`, not `=== 2`. `_MultiSeriesConfig.dimensions`
  // (backend/app/schemas/report_layout.py:157) constrains NO length; the
  // `max_length=MAX_DIMENSIONS` ceiling lives on the QUERY AST
  // (backend/app/schemas/reports_query.py:274). So a layout persists three
  // dimensions happily and only 422s later, at query time. An `=== 2` guard
  // draws that config through the same broken merge.

  it("F17: line refuses THREE dimensions", async () => {
    respond(() => {
      throw new Error("422 too many dimensions");
    });

    const card = await renderLine(makeLine(["month", "category", "account"]));

    expect(
      within(card).getByTestId("line-widget-unsupported"),
    ).toHaveTextContent(NOTICE_FRAGMENT);
    expect(within(card).queryByTestId("line-chart-stub")).toBeNull();
    expect(within(card).queryByTestId("line-widget-error")).toBeNull();
  });

  it("F18: area refuses THREE dimensions", async () => {
    respond(() => {
      throw new Error("422 too many dimensions");
    });

    const card = await renderArea(makeArea(["month", "category", "account"]));

    expect(
      within(card).getByTestId("area-widget-unsupported"),
    ).toHaveTextContent(NOTICE_FRAGMENT);
    expect(within(card).queryByTestId("area-chart-stub")).toBeNull();
    expect(within(card).queryByTestId("area-widget-error")).toBeNull();
  });
});
