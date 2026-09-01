/**
 * The Notice Register — the one place a report widget turns a fact its
 * chart cannot draw into a sentence.
 *
 * ## Severity is a property of the (condition, widget) PAIR
 *
 * Not of the condition. `LIMIT` lands AFTER `GROUP BY`
 * (`reports_query_service.py:334,361`), so every group the server DID
 * return carries its own correct value. Truncation is therefore
 * "correct but incomplete" on a bar, line, area, stacked bar or
 * sparkline — each mark is its own complete group — and it is WRONG on a
 * pie or a table, where the widget composes a cross-row quantity (the
 * donut total, `topNWithOther`'s "Other" slice, the totals row) out of
 * whatever came back.
 *
 * That is why `derivesCrossRowAggregate` is an explicit boolean literal
 * at each call site rather than a lookup keyed on the condition: a
 * condition-keyed severity map cannot express "the same fact, two
 * severities", and it reads as correct right up until a reader acts on a
 * fabricated total.
 *
 * ⚠ Tone is never colour alone. The glyph shape AND the sentence both
 * change with it — see `WidgetNotices.tsx`.
 *
 * ⚠ `meta.warning` is a SERVER-AUTHORED sentence and is rendered
 * verbatim. `backend/app/reports/sources/credit_utilization.py`
 * deliberately composes two notices into ONE string, so paraphrasing it
 * here silently drops one of them.
 *
 * ⚠ Truncation is read from `meta.truncated` ONLY, never inferred from
 * `widget.config.limit` or from `row_count === limit`. A page that is
 * exactly `limit` rows long need not be short.
 */
import type { QueryMeta } from "@/lib/reports/types";

/** The two conditions the register carries. There is no third tenant. */
export type NoticeKind = "truncated" | "source";

export type NoticeTone = "quiet" | "loud";

/**
 * What the RENDERING widget contributes to the severity decision.
 *
 * `derivesCrossRowAggregate` is true when the widget composes a quantity
 * from ACROSS the returned rows — pie (donut total + the "Other" fold),
 * table (the totals row). It is false when every rendered value is one
 * group's own value — bar, line, area, stacked bar, sparkline, kpi.
 */
export interface WidgetNoticeContext {
  derivesCrossRowAggregate: boolean;
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

/** Severity of one condition ON one widget. See the module docstring. */
export function toneFor(
  kind: NoticeKind,
  widget: WidgetNoticeContext,
): NoticeTone {
  if (kind === "source") return "quiet";
  return widget.derivesCrossRowAggregate ? "loud" : "quiet";
}

/**
 * The truncation sentence. It changes with tone, not just its colour.
 *
 * The loud form explains a MISSING number, not a wrong one: under
 * truncation `TableWidget` drops its totals row and `PieWidgetChart`
 * drops the donut total (and its `sr-only` twin) rather than fabricate
 * one. Annotating a wrong figure is the weaker half of the fix.
 */
function truncationText(rowCount: number, tone: NoticeTone): string {
  const head = `Showing the first ${rowCount} rows.`;
  if (tone === "quiet") return head;
  return (
    `${head} The total is hidden because it would cover only those rows, ` +
    `and any shares shown are shares of those rows.`
  );
}

/**
 * Derive the notice set for one widget from the metas of every query it
 * fired (one for single-query widgets, N for multi-series widgets).
 *
 * Returns `null` when there is nothing to say — the ~95% case. Callers
 * render nothing at all: no placeholder, no reserved slot.
 */
export function deriveWidgetNotices(
  metas: Array<QueryMeta | undefined>,
  widget: WidgetNoticeContext,
): WidgetNoticeSet | null {
  const present = metas.filter((m): m is QueryMeta => !!m);

  const notices: WidgetNotice[] = [];

  // ── truncation ────────────────────────────────────────────────────
  // ANY series being short truncates the widget: a multi-measure table
  // whose SECOND query hit the cap renders an incomplete merge just as
  // surely as one whose first did.
  const truncatedMetas = present.filter((m) => m.truncated);
  if (truncatedMetas.length > 0) {
    // The rendered set is the UNION of the series' rows keyed by
    // dimension, so the largest returned count is the closest honest
    // lower bound on "how many rows you are looking at".
    const rowCount = truncatedMetas.reduce(
      (max, m) => Math.max(max, m.row_count),
      0,
    );
    const tone = toneFor("truncated", widget);
    notices.push({ kind: "truncated", tone, text: truncationText(rowCount, tone) });
  }

  // ── source warnings ───────────────────────────────────────────────
  // Verbatim, de-duplicated by exact text: line/area fire N identical
  // queries against ONE source, so the same sentence arrives N times.
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
