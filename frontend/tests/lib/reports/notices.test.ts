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

/** Bar / line / area / sparkline / kpi, and a ONE-dimension bar. */
const OWN_VALUE = {
  derivesCrossRowAggregate: false,
  withholdsCrossRowAggregate: false,
};
/** Pie, table: composes a cross-row quantity AND withholds it. */
const WITHHELD = {
  derivesCrossRowAggregate: true,
  withholdsCrossRowAggregate: true,
};
/** A TWO-dimension bar: the cross-row sums ARE the chart, so they stay. */
const RENDERED = {
  derivesCrossRowAggregate: true,
  withholdsCrossRowAggregate: false,
};

/**
 * A truncated meta that also REPORTS its end. TBD-484 put
 * `truncated_end` on the wire; the client no longer infers it, so a meta
 * without it is the honest-absence case, not the ordinary one.
 */
function cut(
  rowCount: number,
  end: QueryMeta["truncated_end"],
  over: Partial<QueryMeta> = {},
): QueryMeta {
  return meta({ row_count: rowCount, truncated: true, truncated_end: end, ...over });
}

// The exact string `credit_utilization.py` composes when BOTH of its
// notices apply. Held verbatim so a re-worded / paraphrased render fails.
const COMPOSED_SOURCE_WARNING =
  "This organization holds credit cards in more than one currency; " +
  "currencies are never summed, so rows stay partitioned by currency. " +
  "2 credit card(s) excluded — no credit limit set.";

describe("deriveWidgetNotices", () => {
  it("returns null when there is nothing to say (the ~95% case)", () => {
    expect(deriveWidgetNotices([meta()], OWN_VALUE, "ready")).toBeNull();
    expect(deriveWidgetNotices([meta()], WITHHELD, "ready")).toBeNull();
    expect(deriveWidgetNotices([], OWN_VALUE, "ready")).toBeNull();
    expect(deriveWidgetNotices([undefined], OWN_VALUE, "ready")).toBeNull();
  });

  // KILLS: truncation inferred from `widget.config.limit` or from
  // `row_count === limit`. A full page that is NOT truncated says nothing.
  it("reads truncation from meta.truncated ONLY", () => {
    const full = meta({ row_count: 500, truncated: false });
    expect(deriveWidgetNotices([full], OWN_VALUE, "ready")).toBeNull();
    expect(deriveWidgetNotices([full], WITHHELD, "ready")).toBeNull();
  });

  // ── THE DECISIVE FENCE (pure half) ────────────────────────────────
  // KILLS: a condition-keyed severity map. One identical meta, two
  // widgets, three observable differences.
  it("gives ONE meta different tone AND different text per widget kind", () => {
    const m = cut(25, "lowest-ranked");
    const quiet = deriveWidgetNotices([m], OWN_VALUE, "ready") as WidgetNoticeSet;
    const loud = deriveWidgetNotices([m], WITHHELD, "ready") as WidgetNoticeSet;

    expect(quiet.tone).toBe("quiet");
    expect(loud.tone).toBe("loud");
    expect(quiet.summary).not.toBe(loud.summary);
    expect(quiet.notices[0].tone).not.toBe(loud.notices[0].tone);
  });

  it("quiet truncation copy states only that the set is short", () => {
    const set = deriveWidgetNotices(
      [cut(25, "lowest-ranked")],
      OWN_VALUE,
      "ready",
    ) as WidgetNoticeSet;
    expect(set.summary).toBe("Showing the first 25 rows.");
  });

  // KILLS: `${n} rows` unconditionally. `limit: 1` on a KPI makes the
  // singular the commonest case the moment TBD-484 lands.
  it("pluralises the row count", () => {
    const set = deriveWidgetNotices(
      [cut(1, "lowest-ranked")],
      OWN_VALUE,
      "ready",
    ) as WidgetNoticeSet;
    expect(set.summary).toBe("Showing the first 1 row.");
  });

  // The loud sentence must explain the SUPPRESSED total, not annotate a
  // wrong one — the number is gone by the time this renders.
  it("loud+withheld copy explains the withheld total", () => {
    const set = deriveWidgetNotices(
      [cut(25, "lowest-ranked")],
      WITHHELD,
      "ready",
    ) as WidgetNoticeSet;
    expect(set.summary).toBe(
      "Showing the first 25 rows. The total is hidden because it would " +
        "cover only what came back, and any shares shown are shares of that.",
    );
  });

  // ── B3 ────────────────────────────────────────────────────────────
  // KILLS: one loud sentence for every loud widget. A two-dimension bar
  // withholds NOTHING — the partial sums ARE the bars — so "the total is
  // hidden" is false there, and `row_count` counts (primary, secondary)
  // PAIRS, not bars, so "showing the first 500 rows" beside ten bars is
  // a second falsehood.
  it("loud+rendered copy says the drawn values under-report", () => {
    const set = deriveWidgetNotices(
      [cut(500, "lowest-ranked")],
      RENDERED,
      "ready",
    ) as WidgetNoticeSet;
    expect(set.tone).toBe("loud");
    expect(set.summary).toBe(
      "Only the first 500 rows of the break-down were returned. The values " +
        "drawn are sums over only what came back, so they under-report.",
    );
    expect(set.summary).not.toContain("The total is hidden");
  });

  // KILLS: `meta.warning` re-worded, truncated, prefixed or split. The
  // server composes two notices into one string; paraphrasing drops one.
  it("passes meta.warning through VERBATIM", () => {
    const set = deriveWidgetNotices(
      [meta({ warning: COMPOSED_SOURCE_WARNING })],
      OWN_VALUE,
      "ready",
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
      WITHHELD,
      "ready",
    ) as WidgetNoticeSet;
    expect(set.tone).toBe("quiet");
  });

  // ── B2: the two tenants do NOT share an empty-state rule ──────────
  // KILLS: one `suppressed` boolean covering loading/error/empty for both
  // tenants. When every credit card lacks a limit, `credit_utilization`
  // returns ZERO rows WITH the warning set — so a blanket empty-state
  // suppression re-silences the disclosure whose own source comment says
  // "Silent exclusion is not acceptable."
  it("keeps a source warning on an EMPTY result", () => {
    const set = deriveWidgetNotices(
      [meta({ row_count: 0, warning: COMPOSED_SOURCE_WARNING })],
      OWN_VALUE,
      "empty",
    ) as WidgetNoticeSet;
    expect(set).not.toBeNull();
    expect(set.notices.map((n) => n.kind)).toEqual(["source"]);
    expect(set.summary).toBe(COMPOSED_SOURCE_WARNING);
  });

  // KILLS: the opposite over-correction — keeping BOTH tenants on empty.
  // "Showing the first 0 rows" beside "No data" is noise.
  it("drops truncation on an EMPTY result", () => {
    expect(
      deriveWidgetNotices(
        [cut(0, "lowest-ranked")],
        WITHHELD,
        "empty",
      ),
    ).toBeNull();
  });

  it("keeps only the source half when a warning and truncation coincide on empty", () => {
    const set = deriveWidgetNotices(
      [cut(0, "lowest-ranked", { warning: "Source says hello." })],
      WITHHELD,
      "empty",
    ) as WidgetNoticeSet;
    expect(set.notices.map((n) => n.kind)).toEqual(["source"]);
    expect(set.tone).toBe("quiet");
  });

  it("says nothing at all while loading or in the error branch", () => {
    const m = cut(5, "lowest-ranked", { warning: "Anything." });
    expect(deriveWidgetNotices([m], WITHHELD, "loading")).toBeNull();
    expect(deriveWidgetNotices([m], WITHHELD, "error")).toBeNull();
  });

  // KILLS: `useSeriesQueries` reading only series[0]'s meta.
  it("notices when only a LATER series is truncated", () => {
    const set = deriveWidgetNotices(
      [meta({ row_count: 4 }), cut(100, "lowest-ranked")],
      OWN_VALUE,
      "ready",
    ) as WidgetNoticeSet;
    expect(set).not.toBeNull();
    expect(set.summary).toBe("Showing the first 100 rows.");
  });

  // KILLS: Math.max -> Math.min (or `[0]`) when SEVERAL series truncate
  // with DIFFERENT counts. The rendered body is their union, so the
  // largest count is the closest honest lower bound.
  it("reports the LARGEST row_count across several truncated series", () => {
    const set = deriveWidgetNotices(
      [
        cut(40, "lowest-ranked"),
        cut(120, "lowest-ranked"),
        cut(75, "lowest-ranked"),
      ],
      OWN_VALUE,
      "ready",
    ) as WidgetNoticeSet;
    expect(set.summary).toBe("Showing the first 120 rows.");
  });

  // KILLS: naive concat across series. line/area fire N identical queries
  // against ONE source, so the same warning arrives N times.
  it("de-duplicates an identical warning across series", () => {
    const set = deriveWidgetNotices(
      [meta({ warning: "Same notice." }), meta({ warning: "Same notice." })],
      OWN_VALUE,
      "ready",
    ) as WidgetNoticeSet;
    expect(set.notices).toHaveLength(1);
    expect(set.summary).toBe("Same notice.");
  });

  // KILLS: composition in arrival order. The loud sentence must lead —
  // `aria-describedby` flattens the subtree, so ordering is the only
  // structure the reader gets.
  it("composes loud first, then quiet, space-joined", () => {
    const set = deriveWidgetNotices(
      [cut(8, "lowest-ranked", { warning: "Source says hello." })],
      WITHHELD,
      "ready",
    ) as WidgetNoticeSet;
    expect(set.notices.map((n) => n.kind)).toEqual(["truncated", "source"]);
    expect(set.tone).toBe("loud");
    expect(set.summary.startsWith("Showing the first 8 rows.")).toBe(true);
    expect(set.summary.endsWith("Source says hello.")).toBe(true);
    // The join itself: exactly one space between the two sentences.
    expect(set.summary).toContain("that. Source says hello.");
  });

  it("ignores undefined metas from series that have not resolved", () => {
    const set = deriveWidgetNotices(
      [undefined, cut(7, "lowest-ranked")],
      OWN_VALUE,
      "ready",
    ) as WidgetNoticeSet;
    expect(set.summary).toBe("Showing the first 7 rows.");
  });
});

// ── TBD-484: the end is REPORTED, not inferred ─────────────────────
//
// `truncated` is not one fact. The server now says which end its limit
// dropped, because the client could not: `networth` tail-keeps only on
// its time-series branch, and `sort.dir` inverts the answer for the
// ranking sources too (every seeded line/area widget sends
// `sort: {by:"dimension", dir:"asc"}` over `month`, so it keeps the
// OLDEST rows and drops the NEWEST). The whole `(dataset, dimensions)`
// map that used to live here is deleted.
describe("truncation copy names the REPORTED end", () => {
  // THE DECISIVE CROSS-END FENCE. One meta, five ends, five sentences.
  // A single `switch` arm collapsed onto another dies here, and so does
  // any `?? "lowest-ranked"` default.
  it("renders five distinct sentences across the four ends and null", () => {
    const summaries = (
      ["lowest-ranked", "highest-ranked", "oldest", "newest", null] as const
    ).map(
      (end) =>
        (
          deriveWidgetNotices(
            [cut(12, end)],
            OWN_VALUE,
            "ready",
          ) as WidgetNoticeSet
        ).summary,
    );
    expect(new Set(summaries).size).toBe(5);
    expect(summaries[0]).toBe("Showing the first 12 rows.");
    expect(summaries[1]).toBe(
      "Showing the lowest 12 rows; higher-ranked rows are not included.",
    );
    expect(summaries[2]).toBe(
      "Showing the most recent 12 periods; earlier periods in this range " +
        "are not included.",
    );
    expect(summaries[3]).toBe(
      "Showing the earliest 12 periods; later periods in this range are " +
        "not included.",
    );
    expect(summaries[4]).toBe("Showing 12 rows; more matched than are shown.");
  });

  // ⚠⚠ `null` is ROUTINE, not an edge case. Ordering by a non-time
  // dimension is alphabetical and has no reader-facing end, and
  // `accounts` / `recurring` / `credit_utilization` publish NO time
  // dimension at all — so sort-by-name is their only by-dimension shape.
  // KILLS: `meta.truncated_end ?? "lowest-ranked"`.
  it("renders an UNQUALIFIED sentence when the server reports no end", () => {
    for (const m of [cut(12, null), cut(12, undefined)]) {
      const set = deriveWidgetNotices([m], OWN_VALUE, "ready") as WidgetNoticeSet;
      expect(set.summary).toBe("Showing 12 rows; more matched than are shown.");
      expect(set.summary).not.toContain("the first");
      expect(set.summary).not.toContain("most recent");
      expect(set.summary).not.toContain("earliest");
      expect(set.summary).not.toContain("lowest");
    }
  });

  it("pluralises periods as well as rows", () => {
    expect(
      (deriveWidgetNotices([cut(1, "oldest")], OWN_VALUE, "ready") as WidgetNoticeSet)
        .summary,
    ).toContain("the most recent 1 period;");
    expect(
      (deriveWidgetNotices([cut(1, null)], OWN_VALUE, "ready") as WidgetNoticeSet)
        .summary,
    ).toBe("Showing 1 row; more matched than are shown.");
  });

  // ⚠ The chronological wording is scoped to the REQUESTED range. It must
  // not claim data exists outside it — `networth` measures its pre-slice
  // total AFTER windowing, so it knows nothing about anything earlier.
  it("scopes the chronological omission to the requested range", () => {
    for (const end of ["oldest", "newest"] as const) {
      const set = deriveWidgetNotices(
        [cut(12, end)],
        OWN_VALUE,
        "ready",
      ) as WidgetNoticeSet;
      expect(set.summary).toContain("in this range");
      expect(set.summary).not.toMatch(/more data|data exists/i);
    }
  });

  it("composes a chronological head with the withheld tail", () => {
    const set = deriveWidgetNotices(
      [cut(12, "oldest")],
      WITHHELD,
      "ready",
    ) as WidgetNoticeSet;
    expect(set.summary).toBe(
      "Showing the most recent 12 periods; earlier periods in this range " +
        "are not included. The total is hidden because it would cover only " +
        "what came back, and any shares shown are shares of that.",
    );
  });

  it("composes a chronological head with the break-down tail", () => {
    const set = deriveWidgetNotices(
      [cut(500, "newest")],
      RENDERED,
      "ready",
    ) as WidgetNoticeSet;
    expect(set.summary).toBe(
      "Only the earliest 500 periods of the break-down were returned; later " +
        "periods in this range are not included. The values drawn are sums " +
        "over only what came back, so they under-report.",
    );
  });

  it("composes the unqualified head with the break-down tail", () => {
    const set = deriveWidgetNotices(
      [cut(500, null)],
      RENDERED,
      "ready",
    ) as WidgetNoticeSet;
    expect(set.summary).toBe(
      "Only 500 rows of the break-down were returned; more matched than " +
        "are shown. The values drawn are sums over only what came back, so " +
        "they under-report.",
    );
  });

  // The end axis is orthogonal to tone: it changes the SENTENCE, never
  // the severity.
  it("does not change tone", () => {
    for (const end of ["lowest-ranked", "oldest", "newest", null] as const) {
      const m = [cut(9, end)];
      expect(deriveWidgetNotices(m, OWN_VALUE, "ready")!.tone).toBe("quiet");
      expect(deriveWidgetNotices(m, WITHHELD, "ready")!.tone).toBe("loud");
      expect(deriveWidgetNotices(m, RENDERED, "ready")!.tone).toBe("loud");
    }
  });
});

// ⚠ Multi-series widgets fire one query PER MEASURE, and the series can
// come back with DIFFERENT ends.
describe("truncation end across several series", () => {
  it("keeps the end when every truncated series agrees", () => {
    const set = deriveWidgetNotices(
      [cut(40, "oldest"), cut(120, "oldest")],
      OWN_VALUE,
      "ready",
    ) as WidgetNoticeSet;
    expect(set.summary).toContain("the most recent 120 periods");
  });

  // KILLS: reading `metas[0].truncated_end` and calling it the answer.
  // Two series that dropped opposite ends have no honest single end, so
  // the sentence must name none — the same degradation `sharedFormatFor`
  // performs on mixed formats (TBD-403).
  it("collapses DISAGREEING ends to the unqualified sentence", () => {
    const set = deriveWidgetNotices(
      [cut(40, "oldest"), cut(120, "newest")],
      OWN_VALUE,
      "ready",
    ) as WidgetNoticeSet;
    expect(set.summary).toBe("Showing 120 rows; more matched than are shown.");
  });

  // A partially-reported set is as unanswerable as a disagreeing one —
  // again exactly `sharedFormatFor`'s rule.
  it("collapses a partially-reported set to the unqualified sentence", () => {
    const set = deriveWidgetNotices(
      [cut(40, "oldest"), cut(120, null)],
      OWN_VALUE,
      "ready",
    ) as WidgetNoticeSet;
    expect(set.summary).toBe("Showing 120 rows; more matched than are shown.");
  });

  // ⚠ Only the TRUNCATED series get a vote. A series that came back whole
  // reports no end, and letting it veto would silence the qualification
  // on every mixed widget.
  it("ignores the end of series that were not truncated", () => {
    const set = deriveWidgetNotices(
      [meta({ row_count: 4 }), cut(120, "oldest")],
      OWN_VALUE,
      "ready",
    ) as WidgetNoticeSet;
    expect(set.summary).toContain("the most recent 120 periods");
  });
});

describe("toneFor", () => {
  it("is a function of the (condition, widget) PAIR", () => {
    expect(toneFor("truncated", WITHHELD)).toBe("loud");
    expect(toneFor("truncated", RENDERED)).toBe("loud");
    expect(toneFor("truncated", OWN_VALUE)).toBe("quiet");
    expect(toneFor("source", WITHHELD)).toBe("quiet");
    expect(toneFor("source", OWN_VALUE)).toBe("quiet");
  });
});
