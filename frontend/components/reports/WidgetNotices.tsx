"use client";

/**
 * The Notice Register's rendered surface — ONE glyph per widget, mounted
 * INLINE in the widget's existing header row between the title and
 * `WidgetCsvButton`.
 *
 * ⚠ NOT the card corner. `WidgetShell.tsx` already parks the drag /
 * remove overlay there absolutely at `right-1 top-1 z-10`.
 *
 * ⚠ And not the header row's RIGHT cluster either, next to
 * `WidgetCsvButton`. That overlay is absolute and ~48px wide, and the
 * widget card's own `p-4` puts the right cluster at 16-42px from the
 * card edge — squarely underneath it whenever the widget has no filter
 * chips (the common case), which is exactly why `WidgetCsvButton` hides
 * itself in edit mode. The notice must NOT hide in edit mode: it is the
 * only thing that explains a total the editor can see is missing. So it
 * sits immediately after the TITLE instead, where the overlay never
 * reaches and both modes can show it.
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
import { deriveWidgetNotices } from "@/lib/reports/notices";
import type { QueryMeta } from "@/lib/reports/types";

interface Props {
  /** One entry per query the widget fired. Unresolved series pass `undefined`. */
  metas: Array<QueryMeta | undefined>;
  /**
   * True when the widget composes a quantity from ACROSS the returned
   * rows (pie, table). Drives truncation's severity — see
   * `lib/reports/notices.ts`.
   */
  derivesCrossRowAggregate: boolean;
  /** The widget's displayed title; part of the button's accessible name. */
  widgetTitle: string;
  /**
   * Loading / error / empty. A notice about the SHAPE of data that failed
   * to arrive is noise, so the register goes silent in all three.
   */
  suppressed: boolean;
}

export default function WidgetNotices({
  metas,
  derivesCrossRowAggregate,
  widgetTitle,
  suppressed,
}: Props) {
  const set = useMemo(
    () =>
      suppressed ? null : deriveWidgetNotices(metas, { derivesCrossRowAggregate }),
    [suppressed, metas, derivesCrossRowAggregate],
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
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions, jsx-a11y/click-events-have-key-events
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
