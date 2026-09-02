/**
 * The Notice Register — the one place a report widget turns a fact its
 * chart cannot draw into a sentence.
 *
 * ## Severity is a property of the (condition, widget) PAIR
 *
 * Not of the condition. `LIMIT` lands AFTER `GROUP BY`
 * (`reports_query_service.py:334,361`), so every group the server DID
 * return carries its own correct value. Truncation is therefore
 * "correct but incomplete" — quiet — on a kpi, line, area, sparkline or
 * SINGLE-dimension bar / stacked bar, where each mark is its own
 * complete group. It is WRONG — loud — wherever the widget composes a
 * quantity from ACROSS the returned rows:
 *
 *   - pie (donut total, `topNWithOther`'s "Other" fold) and table (the
 *     totals row): the composed quantity is WITHHELD under truncation.
 *   - a TWO-dimension bar / stacked bar: `astLimitForBarFamily` asks for
 *     `limit: 500` because with two dimensions a row is a (primary,
 *     secondary) PAIR (`breakdown.ts`), so past 500 pairs
 *     `capPrimaryBuckets` ranks on a partial `rowTotal`, the "Other"
 *     fold sums a partial tail, and every bar height is a partial sum.
 *     Nothing can be withheld there: the composed sums ARE the chart.
 *
 * That is why the two flags below are explicit at each call site rather
 * than a lookup keyed on the condition: a condition-keyed severity map
 * cannot express "the same fact, two severities", and it reads as
 * correct right up until a reader acts on a fabricated total.
 *
 * ## `truncated` does NOT mean the same thing on every source
 *
 * Which end the limit dropped is REPORTED, on `meta.truncated_end`
 * (TBD-484). It is not inferred here, and must not be: the client tried
 * and was wrong twice. `networth` tail-keeps only on its time-series
 * branch, so a `(dataset, dimensions)` map got a networth KPI and a
 * networth pie-by-currency backwards; and `sort.dir` inverts the answer
 * for the ranking sources too, so every seeded line / area / stacked-bar
 * widget — `sort: {by: "dimension", dir: "asc"}` over `month` — keeps the
 * OLDEST rows and drops the NEWEST, which no source-keyed map could see.
 * Only the server knows its own effective ordering.
 *
 * ⚠⚠ `truncated_end` is ABSENT far more often than it looks, and that is
 * ROUTINE. Ordering by a non-time dimension is alphabetical and has no
 * reader-facing end, so the backend honestly returns `null` — and
 * `accounts`, `recurring` and `credit_utilization` publish no time
 * dimension at all, which makes sort-by-name their ONLY by-dimension
 * shape. `{truncated: true, truncated_end: null}` is a NORMAL answer for
 * a name-sorted table, not a defect. It renders the unqualified sentence.
 * ⚠ Never `?? "lowest-ranked"`: an honest absence beats a confident
 * guess, and that particular guess is wrong for a whole class of
 * ordinary tables.
 *
 * ⚠ The chronological wording is scoped to the range the user asked for
 * ("earlier periods IN THIS RANGE"), never "more data exists".
 * `networth` measures its pre-slice total AFTER windowing, so it knows
 * nothing about anything outside that window.
 *
 * ⚠ Tone is never colour alone. The glyph shape AND the sentence both
 * change with it — see `WidgetNotices.tsx`.
 *
 * ⚠ `meta.warning` is a SERVER-AUTHORED sentence and is rendered
 * verbatim. `backend/app/reports/sources/credit_utilization.py`
 * deliberately composes two notices into ONE string, so paraphrasing it
 * here silently drops one of them.
 *
 * ⚠ The two tenants do NOT share an empty-state rule. `truncated` is
 * dropped on an empty result ("showing the first 0 rows" beside "No
 * data" is noise); `warning` is KEPT, because the empty result can be
 * exactly what the warning explains — when every credit card lacks a
 * limit, `credit_utilization` returns zero rows WITH its
 * excluded-card disclosure set, and that source's own comment says
 * "Silent exclusion is not acceptable."
 *
 * ⚠ This module never INFERS truncation — not from `widget.config.limit`,
 * not from `row_count === limit`. Do NOT add a second inference here: the
 * flag is authoritative and the frontend cannot tell a true positive from
 * a false one.
 *
 * It is now trustworthy. TBD-484 (`cd5af9b6`) fixed the three SQL-limited
 * sources, which computed `truncated = len(out_rows) >= limit` against
 * rows the DATABASE had already limited — true for any complete page that
 * exactly filled the limit, and unconditionally true for a KPI
 * (`limit: 1`, one row back). They now over-fetch one row and compare
 * `> limit`.
 */
import type { QueryMeta, TruncatedEnd } from "@/lib/reports/types";

/** The two conditions the register carries. There is no third tenant. */
export type NoticeKind = "truncated" | "source";

export type NoticeTone = "quiet" | "loud";

/**
 * The widget's render state. Not a single `suppressed` boolean, because
 * the two tenants diverge on exactly one of these values — see the
 * empty-state note in the module docstring.
 */
export type WidgetDataState = "loading" | "error" | "empty" | "ready";

/**
 * What the RENDERING widget contributes to the severity decision.
 *
 * `derivesCrossRowAggregate` — true when the widget composes a quantity
 * from ACROSS the returned rows. Decides TONE.
 *
 * `withholdsCrossRowAggregate` — only meaningful when the first is true.
 * True when the widget DROPS that quantity under truncation (pie's donut
 * total, table's totals row) so the reader cannot act on it; false when
 * the composed quantity IS the chart and cannot be dropped (a
 * two-dimension bar's stack heights). Decides the loud SENTENCE: one
 * explains a number that is gone, the other warns about numbers that are
 * still on screen and under-report.
 */
export interface WidgetNoticeContext {
  derivesCrossRowAggregate: boolean;
  withholdsCrossRowAggregate: boolean;
}

export interface WidgetNotice {
  kind: NoticeKind;
  tone: NoticeTone;
  /** The exact sentence shown. Never re-worded downstream. */
  text: string;
}

export interface WidgetNoticeSet {
  /** Loud notices first, then quiet — the order `summary` is composed in. */
  notices: WidgetNotice[];
  /** The set's tone: loud when ANY notice is loud. Drives glyph + colour. */
  tone: NoticeTone;
  /**
   * The whole register as ONE string.
   *
   * ⚠ Not a list. `role="tooltip"` is surfaced through
   * `aria-describedby`, which flattens the subtree, so `<ul>` structure
   * is lost to assistive tech. Ordering is the only structure a reader
   * gets, which is why the loud sentence leads.
   */
  summary: string;
}

/**
 * Map a widget's three render branches onto the state the register reads.
 *
 * The ORDER mirrors every widget's own JSX branch order
 * (`isLoading ? … : error ? … : empty ? … : chart`) so the two stay in
 * step by inspection.
 *
 * ⚠ No test can see that ordering today, and pretending otherwise would
 * be a fence that asserts nothing: `loading` and `error` both derive to
 * `null`, and SWR cannot report a first-fetch error and `isLoading` for
 * the same widget anyway. Swapping the two lines is an EQUIVALENT mutant.
 * It becomes observable only if the two states ever diverge — at which
 * point this comment is the thing to re-read.
 */
export function widgetDataState(
  isLoading: boolean,
  error: unknown,
  hasRows: boolean,
): WidgetDataState {
  if (isLoading) return "loading";
  if (error) return "error";
  return hasRows ? "ready" : "empty";
}

/**
 * The one end every truncated series agrees on, or `null`.
 *
 * ⚠ Multi-series widgets fire one query PER MEASURE and the series can
 * come back having dropped DIFFERENT ends. There is no honest single
 * answer then, so the set degrades to the unqualified sentence — exactly
 * what `sharedFormatFor` does with mixed formats (TBD-403), and for the
 * same reason: "a partially-resolved set is as unanswerable as a
 * disagreeing one". A partially-REPORTED set (one series `null`)
 * degrades identically.
 *
 * ⚠ Only TRUNCATED series get a vote. A series that came back whole
 * reports no end, and letting it veto would silence the qualification on
 * every mixed widget.
 */
function agreedTruncatedEnd(
  truncatedMetas: QueryMeta[],
): TruncatedEnd | null {
  const first = truncatedMetas[0]?.truncated_end ?? null;
  if (first === null) return null;
  return truncatedMetas.every((m) => m.truncated_end === first) ? first : null;
}

/** Severity of one condition ON one widget. See the module docstring. */
export function toneFor(
  kind: NoticeKind,
  widget: WidgetNoticeContext,
): NoticeTone {
  if (kind === "source") return "quiet";
  return widget.derivesCrossRowAggregate ? "loud" : "quiet";
}

/**
 * The truncation sentence.
 *
 * Composed as HEAD + optional TAIL so the three axes stay orthogonal:
 * the head names what you are looking at (reported end × break-down), the
 * tail names what that does to the numbers (withhold × render).
 *
 * ⚠ Five heads, not four. `end === null` is the honest-absence case and
 * gets a sentence that names NO end at all — see the module docstring for
 * why that is routine rather than exceptional.
 *
 * ⚠ A break-down's rows are (primary, secondary) PAIRS, so they are never
 * "the bars you can see" and must not be described as if they were.
 */
function truncationText(
  rowCount: number,
  tone: NoticeTone,
  widget: WidgetNoticeContext,
  end: TruncatedEnd | null,
): string {
  const isBreakdown =
    widget.derivesCrossRowAggregate && !widget.withholdsCrossRowAggregate;
  const rows = `${rowCount} ${rowCount === 1 ? "row" : "rows"}`;
  const periods = `${rowCount} ${rowCount === 1 ? "period" : "periods"}`;

  // `kept` describes what IS shown; `omission` names the end that went.
  // The four values name what the limit DROPPED, so each pairs with the
  // opposite end being on screen.
  let kept: string;
  let omission: string;
  switch (end) {
    case "lowest-ranked":
      // The default ordering (value DESC) — "the first" already reads as
      // the top, so no omission clause is needed to be unambiguous.
      kept = `the first ${rows}`;
      omission = "";
      break;
    case "highest-ranked":
      // value ASC: rare and counter-intuitive, so it says which end went.
      kept = `the lowest ${rows}`;
      omission = "; higher-ranked rows are not included";
      break;
    case "oldest":
      kept = `the most recent ${periods}`;
      omission = "; earlier periods in this range are not included";
      break;
    case "newest":
      kept = `the earliest ${periods}`;
      omission = "; later periods in this range are not included";
      break;
    default:
      kept = rows;
      omission = "; more matched than are shown";
  }

  const head = isBreakdown
    ? `Only ${kept} of the break-down were returned${omission}.`
    : `Showing ${kept}${omission}.`;

  if (tone === "quiet") return head;
  if (widget.withholdsCrossRowAggregate) {
    return (
      `${head} The total is hidden because it would cover only what came ` +
      `back, and any shares shown are shares of that.`
    );
  }
  return (
    `${head} The values drawn are sums over only what came back, so they ` +
    `under-report.`
  );
}

/**
 * Derive the notice set for one widget from the metas of every query it
 * fired (one for single-query widgets, N for multi-series widgets) and
 * the widget's render state.
 *
 * Returns `null` when there is nothing to say — the ~95% case. Callers
 * render nothing at all: no placeholder, no reserved slot.
 */
export function deriveWidgetNotices(
  metas: Array<QueryMeta | undefined>,
  widget: WidgetNoticeContext,
  state: WidgetDataState,
): WidgetNoticeSet | null {
  // Nothing arrived, or nothing usable did. A notice about the SHAPE of
  // data that failed to arrive is noise.
  if (state === "loading" || state === "error") return null;

  const present = metas.filter((m): m is QueryMeta => !!m);

  const notices: WidgetNotice[] = [];

  // ── truncation ────────────────────────────────────────────────────
  // ANY series being short truncates the widget: a multi-measure table
  // whose SECOND query hit the cap renders an incomplete merge just as
  // surely as one whose first did. Dropped entirely on an empty result.
  const truncatedMetas =
    state === "empty" ? [] : present.filter((m) => m.truncated);
  if (truncatedMetas.length > 0) {
    // The rendered set is the UNION of the series' rows keyed by
    // dimension, so the largest returned count is the closest honest
    // lower bound on "how many rows you are looking at".
    const rowCount = truncatedMetas.reduce(
      (max, m) => Math.max(max, m.row_count),
      0,
    );
    const tone = toneFor("truncated", widget);
    notices.push({
      kind: "truncated",
      tone,
      text: truncationText(
        rowCount,
        tone,
        widget,
        agreedTruncatedEnd(truncatedMetas),
      ),
    });
  }

  // ── source warnings ───────────────────────────────────────────────
  // Verbatim, de-duplicated by exact text: line/area fire N identical
  // queries against ONE source, so the same sentence arrives N times.
  // Survives the empty state deliberately — see the module docstring.
  const seen = new Set<string>();
  for (const m of present) {
    const text = m.warning;
    if (!text || seen.has(text)) continue;
    seen.add(text);
    notices.push({ kind: "source", tone: toneFor("source", widget), text });
  }

  if (notices.length === 0) return null;

  const ordered = [
    ...notices.filter((n) => n.tone === "loud"),
    ...notices.filter((n) => n.tone !== "loud"),
  ];
  return {
    notices: ordered,
    tone: ordered.some((n) => n.tone === "loud") ? "loud" : "quiet",
    summary: ordered.map((n) => n.text).join(" "),
  };
}
