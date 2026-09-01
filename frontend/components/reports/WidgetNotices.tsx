"use client";

/**
 * The Notice Register's rendered surface — ONE glyph per widget, mounted
 * INLINE in the widget's existing header row between the title and
 * `WidgetCsvButton`.
 *
 * ⚠ NOT the card corner. `WidgetShell.tsx` already parks the drag /
 * remove overlay there absolutely at `right-1 top-1 z-10`.
 *
 * ⚠⚠ Sitting after the TITLE is NOT on its own enough, and an earlier
 * revision of this comment claimed otherwise. Measured against
 * `WidgetShell`: the overlay is `right-1 top-1` with two `p-1` + 14px
 * controls, so it occupies x ∈ [W−52, W−4], y ∈ [4, 26], and Remove
 * ALONE occupies x ∈ [W−26, W−4]. In edit mode `WidgetCsvButton` returns
 * `null`, so the title group (`flex-1 min-w-0`) spans the row and pushes
 * this `shrink-0` glyph flush right: with the card's `p-4` that is
 * x ∈ [W−42, W−16], y ∈ [16, 42] — a 10×10px intersection with REMOVE.
 * Clicking the glyph's top-right corner deleted the widget, with no
 * confirmation, and it also voided the 26×16 unobstructed-target claim
 * below. The fix is geometric, not positional: every widget's header row
 * reserves `pr-12` (48px) in edit mode, which puts the glyph's right edge
 * at W−64, clear of the overlay's W−52 by 12px.
 *
 * The notice must NOT take `WidgetCsvButton`'s edit-mode opt-out: it is
 * the only thing that explains a total the editor can see is missing.
 *
 * ## Why `Tooltip`, not `@floating-ui/react`
 *
 * `WidgetEditorPopover` uses floating-ui because it owns a
 * `role="dialog"` with interactive content. This is one sentence of
 * read-only text, so it takes the shared `Tooltip` through its `trigger`
 * prop: `Tooltip` portals to `document.body` (no transformed ancestor to
 * fight), `getBoundingClientRect()` already bakes in react-grid-layout's
 * `transform`, and its scroll listener is capture-phase so it follows the
 * canvas column's inner `overflow-y-auto`.
 *
 * ⚠ Never pass `learnMoreSection`. It is the only interactive content
 * `Tooltip` renders; `role="tooltip"` must not contain interactive
 * elements, and the portal renders at end-of-body so the link is
 * keyboard-unreachable anyway.
 *
 * ## Accessibility
 *
 * - The button's name carries tone + widget title + count. Twelve
 *   buttons all named "More info" in a screen-reader's element list is
 *   worse than silence.
 * - An ALWAYS-PRESENT `sr-only` copy of the summary sits beside the
 *   trigger, because `Tooltip` wires `aria-describedby` only while open.
 * - `p-1.5` + a `h-3.5 w-3.5` glyph is a 26px target, clearing WCAG 2.2
 *   SC 2.5.8. ⚠ Do NOT copy `WidgetCsvButton`'s `p-1` (22px, fails).
 * - No focus class: the `:focus-visible` brass outline has been global
 *   since TBD-319, so a bare `<button>` is already compliant.
 * - No tint / no `-dim` background. The badge tint family does not clear
 *   AA on the light theme; a bare glyph on `bg-surface` sidesteps it.
 */
import { useMemo } from "react";
import { Info, TriangleAlert } from "lucide-react";

import Tooltip from "@/components/Tooltip";
import {
  deriveWidgetNotices,
  type WidgetDataState,
} from "@/lib/reports/notices";
import type { QueryMeta } from "@/lib/reports/types";

interface Props {
  /** One entry per query the widget fired. Unresolved series pass `undefined`. */
  metas: Array<QueryMeta | undefined>;
  /**
   * True when the widget composes a quantity from ACROSS the returned
   * rows — pie, table, and a TWO-dimension bar. Drives truncation's
   * severity; see `lib/reports/notices.ts`.
   */
  derivesCrossRowAggregate: boolean;
  /**
   * True when that composed quantity is WITHHELD under truncation (pie,
   * table); false when it is the chart itself (a two-dimension bar).
   * Chooses which loud sentence is true.
   */
  withholdsCrossRowAggregate: boolean;
  /** The widget's displayed title; part of the button's accessible name. */
  widgetTitle: string;
  /**
   * The widget's render state. ⚠ NOT a `suppressed` boolean: the two
   * tenants diverge on `"empty"`, where a source warning is often the
   * explanation FOR the emptiness and must survive.
   */
  state: WidgetDataState;
}

export default function WidgetNotices({
  metas,
  derivesCrossRowAggregate,
  withholdsCrossRowAggregate,
  widgetTitle,
  state,
}: Props) {
  const set = useMemo(
    () =>
      deriveWidgetNotices(
        metas,
        { derivesCrossRowAggregate, withholdsCrossRowAggregate },
        state,
      ),
    [metas, derivesCrossRowAggregate, withholdsCrossRowAggregate, state],
  );

  if (!set) return null;

  const loud = set.tone === "loud";
  const Glyph = loud ? TriangleAlert : Info;
  const count = set.notices.length;
  const label = `${loud ? "Data warning" : "Data note"} for ${widgetTitle}: ${count} ${
    count === 1 ? "note" : "notes"
  }`;

  return (
    // `WidgetShell` wraps every widget in `onClick={onSelect}`, so without
    // this the notice would also open the widget-config popover.
    <span
      className="inline-flex shrink-0 items-center"
      onClick={(e) => e.stopPropagation()}
    >
      <Tooltip
        content={set.summary}
        trigger={
          <button
            type="button"
            data-testid="widget-notices"
            data-tone={set.tone}
            data-icon={loud ? "triangle-alert" : "info"}
            aria-label={label}
            className={`rounded p-1.5 ${
              loud
                ? "text-warning hover:bg-surface-raised"
                : "text-text-muted hover:bg-surface-raised hover:text-text-primary"
            }`}
          >
            <Glyph aria-hidden="true" className="h-3.5 w-3.5" />
          </button>
        }
      />
      <span data-testid="widget-notices-summary" className="sr-only">
        {set.summary}
      </span>
    </span>
  );
}
