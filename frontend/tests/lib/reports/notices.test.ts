/**
 * TBD-430 — The Notice Register, pure derivation layer.
 *
 * Every test here names the wrong implementation it kills. The decisive
 * one is `severity is a property of the (condition, widget) PAIR`: a
 * condition-keyed severity map (`{truncated: "loud", source: "quiet"}`)
 * passes every single-context test in this file and dies only on the
 * pair test.
 */
import {
  deriveWidgetNotices,
  toneFor,
  type WidgetNoticeSet,
} from "@/lib/reports/notices";
import type { QueryMeta } from "@/lib/reports/types";

function meta(over: Partial<QueryMeta> = {}): QueryMeta {
  return { row_count: 10, truncated: false, query_ms: 3, ...over };
}

const CROSS_ROW = { derivesCrossRowAggregate: true };
const OWN_VALUE = { derivesCrossRowAggregate: false };

// The exact string `credit_utilization.py` composes when BOTH of its
// notices apply. Held verbatim so a re-worded / paraphrased render fails.
const COMPOSED_SOURCE_WARNING =
  "This organization holds credit cards in more than one currency; " +
  "currencies are never summed, so rows stay partitioned by currency. " +
  "2 credit card(s) excluded — no credit limit set.";

describe("deriveWidgetNotices", () => {
  it("returns null when there is nothing to say (the ~95% case)", () => {
    expect(deriveWidgetNotices([meta()], OWN_VALUE)).toBeNull();
    expect(deriveWidgetNotices([meta()], CROSS_ROW)).toBeNull();
    expect(deriveWidgetNotices([], OWN_VALUE)).toBeNull();
    expect(deriveWidgetNotices([undefined], OWN_VALUE)).toBeNull();
  });

  // KILLS: truncation inferred from `widget.config.limit` or from
  // `row_count === limit`. A full page that is NOT truncated says nothing.
  it("reads truncation from meta.truncated ONLY", () => {
    const full = meta({ row_count: 500, truncated: false });
    expect(deriveWidgetNotices([full], OWN_VALUE)).toBeNull();
    expect(deriveWidgetNotices([full], CROSS_ROW)).toBeNull();
  });

  // ── THE DECISIVE FENCE (pure half) ────────────────────────────────
  // KILLS: a condition-keyed severity map. One identical meta, two
  // widgets, three observable differences.
  it("gives ONE meta different tone AND different text per widget kind", () => {
    const m = meta({ row_count: 25, truncated: true });
    const quiet = deriveWidgetNotices([m], OWN_VALUE) as WidgetNoticeSet;
    const loud = deriveWidgetNotices([m], CROSS_ROW) as WidgetNoticeSet;

    expect(quiet.tone).toBe("quiet");
    expect(loud.tone).toBe("loud");
    expect(quiet.summary).not.toBe(loud.summary);
    expect(quiet.notices[0].tone).not.toBe(loud.notices[0].tone);
  });

  it("quiet truncation copy states only that the set is short", () => {
    const set = deriveWidgetNotices(
      [meta({ row_count: 25, truncated: true })],
      OWN_VALUE,
    ) as WidgetNoticeSet;
    expect(set.summary).toBe("Showing the first 25 rows.");
  });

  // The loud sentence must explain the SUPPRESSED total, not annotate a
  // wrong one — the number is gone by the time this renders.
  it("loud truncation copy explains the withheld total", () => {
    const set = deriveWidgetNotices(
      [meta({ row_count: 25, truncated: true })],
      CROSS_ROW,
    ) as WidgetNoticeSet;
    expect(set.summary).toBe(
      "Showing the first 25 rows. The total is hidden because it would " +
        "cover only those rows, and any shares shown are shares of those rows.",
    );
  });

  // KILLS: `meta.warning` re-worded, truncated, prefixed or split. The
  // server composes two notices into one string; paraphrasing drops one.
  it("passes meta.warning through VERBATIM", () => {
    const set = deriveWidgetNotices(
      [meta({ warning: COMPOSED_SOURCE_WARNING })],
      OWN_VALUE,
    ) as WidgetNoticeSet;
    expect(set.notices).toHaveLength(1);
    expect(set.notices[0].kind).toBe("source");
    expect(set.notices[0].text).toBe(COMPOSED_SOURCE_WARNING);
    expect(set.summary).toBe(COMPOSED_SOURCE_WARNING);
  });

  // KILLS: tone derived from the CONDITION alone in the other direction —
  // a source warning is quiet even on a pie/table.
  it("keeps a source warning quiet on a cross-row-aggregate widget", () => {
    const set = deriveWidgetNotices(
      [meta({ warning: COMPOSED_SOURCE_WARNING })],
      CROSS_ROW,
    ) as WidgetNoticeSet;
    expect(set.tone).toBe("quiet");
  });

  // KILLS: `useSeriesQueries` reading only series[0]'s meta.
  it("notices when only a LATER series is truncated", () => {
    const set = deriveWidgetNotices(
      [meta({ row_count: 4 }), meta({ row_count: 100, truncated: true })],
      OWN_VALUE,
    ) as WidgetNoticeSet;
    expect(set).not.toBeNull();
    expect(set.summary).toBe("Showing the first 100 rows.");
  });

  // KILLS: naive concat across series. line/area fire N identical queries
  // against ONE source, so the same warning arrives N times.
  it("de-duplicates an identical warning across series", () => {
    const set = deriveWidgetNotices(
      [meta({ warning: "Same notice." }), meta({ warning: "Same notice." })],
      OWN_VALUE,
    ) as WidgetNoticeSet;
    expect(set.notices).toHaveLength(1);
    expect(set.summary).toBe("Same notice.");
  });

  // KILLS: composition in arrival order. The loud sentence must lead —
  // `aria-describedby` flattens the subtree, so ordering is the only
  // structure the reader gets.
  it("composes loud first, then quiet, space-joined", () => {
    const set = deriveWidgetNotices(
      [meta({ row_count: 8, truncated: true, warning: "Source says hello." })],
      CROSS_ROW,
    ) as WidgetNoticeSet;
    expect(set.notices.map((n) => n.kind)).toEqual(["truncated", "source"]);
    expect(set.tone).toBe("loud");
    expect(set.summary.startsWith("Showing the first 8 rows.")).toBe(true);
    expect(set.summary.endsWith("Source says hello.")).toBe(true);
    expect(set.summary).toContain("rows. Source says hello.");
  });

  it("ignores undefined metas from series that have not resolved", () => {
    const set = deriveWidgetNotices(
      [undefined, meta({ row_count: 7, truncated: true })],
      OWN_VALUE,
    ) as WidgetNoticeSet;
    expect(set.summary).toBe("Showing the first 7 rows.");
  });
});

describe("toneFor", () => {
  it("is a function of the (condition, widget) PAIR", () => {
    expect(toneFor("truncated", CROSS_ROW)).toBe("loud");
    expect(toneFor("truncated", OWN_VALUE)).toBe("quiet");
    expect(toneFor("source", CROSS_ROW)).toBe("quiet");
    expect(toneFor("source", OWN_VALUE)).toBe("quiet");
  });
});
