"use client";

/**
 * The DOM colour key under a report chart (TBD-382 for bar and stacked bar,
 * TBD-427 for line, area and pie). Rendered by the widget, outside the
 * recharts subtree, rather than via recharts' `<Legend>`, because that
 * legend:
 *
 * - colours each label's TEXT with the series hue, and chart tokens are tuned
 *   for 3:1 non-text contrast, not 4.5:1 text (five of eight light-theme hues
 *   fail as text);
 * - sorts its items alphabetically by default (`itemSorter: "value"`), so the
 *   key disagrees with the series / slice order and a folded "Other" is not
 *   last;
 * - carries no accessible name.
 *
 * Here the label inherits `text-text-secondary` and only the swatch carries
 * the hue. Items render in the order given.
 *
 * ⚠ Keys are the INDEX, deliberately. Labels are not unique: pie's
 * `topNWithOther` appends a literal "Other", which collides with a real
 * category of that name ranking inside the top N.
 */

export interface WidgetLegendItem {
  label: string;
  /** A theme-token colour, e.g. `var(--color-chart-1)`. */
  color: string;
}

interface Props {
  /** Testids become `${prefix}-legend`, `-legend-item`, `-legend-swatch`. */
  testidPrefix: string;
  /** Accessible name of the list: what the colours key. */
  label: string;
  items: WidgetLegendItem[];
}

export default function WidgetLegend({ testidPrefix, label, items }: Props) {
  return (
    // TBD-430: capped + scrollable. Uncapped, this `flex-wrap` list grew
    // without bound and bled out of the card on a wide break-down.
    // `tabIndex={0}` is not decoration: WCAG 2.1.1 requires a scrollable
    // region to be keyboard-scrollable, and the list carries an accessible
    // name. No notice accompanies this: a scrollbar says "there is more"
    // natively, and the colour key stays reachable where it belongs.
    <ul
      data-testid={`${testidPrefix}-legend`}
      aria-label={label}
      tabIndex={0}
      className="mt-2 flex max-h-16 flex-wrap gap-x-3 gap-y-1 overflow-y-auto text-xs text-text-secondary"
    >
      {items.map((item, i) => (
        <li
          key={i}
          data-testid={`${testidPrefix}-legend-item`}
          className="flex items-center gap-1"
        >
          <span
            data-testid={`${testidPrefix}-legend-swatch`}
            data-color={item.color}
            aria-hidden="true"
            // The FILL carries the 1.4.11 contrast: swatch vs surface is
            // >= 3.13:1 on every palette hue in both themes, and
            // `border-strong` ("Other") is 3.31 / 3.32. The `ring-border`
            // hairline is 1.35 / 1.31 against the surface, so it bounds
            // nothing on its own; it only softens the edge.
            // `shrink-0`: a wrapping label on a narrow tile would otherwise
            // squash the swatch.
            className="inline-block h-2.5 w-2.5 shrink-0 rounded-sm ring-1 ring-border"
            style={{ backgroundColor: item.color }}
          />
          <span>{item.label}</span>
        </li>
      ))}
    </ul>
  );
}
