import { HelpCircle } from "lucide-react";

/**
 * HelpAnchor — small "?" icon that links to a section of the in-app
 * `/docs` user manual. Sits next to a page title (or other prominent
 * heading) and opens the matching anchor in a new tab so the user keeps
 * their context.
 *
 * Convention: `section` must match a real `id=` on a heading inside
 * `frontend/app/docs/page.tsx`. A broken anchor here is a bug, not a
 * design choice — the L5.3 contract is "every help marker resolves to
 * a real section."
 *
 * Token-clean per `scripts/check-design-tokens.sh`: muted by default,
 * accent on hover/focus. Touch target ≥44px on mobile (WCAG 2.5.8) and
 * compact 28px at desktop where the cursor is precise.
 *
 * Placement variants (PR fix/help-anchor-placement-uniform):
 *
 * - `inline-title` (default): for HelpAnchors next to a page H1.
 *   Self-aligns to the top of the flex row via `self-start` + a small
 *   `mt-1` nudge so the `?` lands at the cap height of the heading
 *   instead of the baseline. Owner spec: "on top of the headers."
 *
 * - `card-corner`: for HelpAnchors that live inside a `relative` card
 *   container. Absolutely positions at `top-3 right-3` so the `?` is
 *   docked in the card's top-right corner, INSIDE the card border.
 *   Owner spec: "inside the cards."
 */
export type HelpAnchorVariant = "inline-title" | "card-corner";

type HelpAnchorProps = {
  /** /docs section id, e.g. "dashboard". Must exist in the docs page. */
  section: string;
  /** Optional extra context for screen readers. Falls back to the section name. */
  label?: string;
  /** Placement variant. Default `inline-title` for header rows. */
  variant?: HelpAnchorVariant;
  /** Optional class overrides for layout fit (margin, alignment). */
  className?: string;
};

// Shared visual + a11y classes. Held constant across variants so the
// `?` icon reads the same wherever it appears; only positioning shifts.
const BASE_CLASSES =
  "inline-flex min-h-[44px] min-w-[44px] md:min-h-7 md:min-w-7 items-center justify-center rounded-full text-text-muted hover:text-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30";

const VARIANT_CLASSES: Record<HelpAnchorVariant, string> = {
  // `self-start` overrides any items-center on the parent flex row, so
  // the icon top-aligns with the heading text regardless of how the
  // parent wraps it. `mt-1` lifts it to the heading's cap height
  // without inflating the row's total height.
  "inline-title": "self-start mt-1",
  // Docks inside the top-right corner of a positioned-relative card.
  // `top-3 right-3` matches the spacing rhythm of the existing
  // AccountMonthEndForecast tile, which is the correct reference.
  "card-corner": "absolute top-3 right-3",
};

export default function HelpAnchor({
  section,
  label,
  variant = "inline-title",
  className = "",
}: HelpAnchorProps) {
  const ariaLabel = `Help: ${label ?? section}`;
  return (
    <a
      href={`/docs#${section}`}
      target="_blank"
      rel="noopener noreferrer"
      aria-label={ariaLabel}
      data-testid="help-anchor"
      data-section={section}
      data-variant={variant}
      className={`${BASE_CLASSES} ${VARIANT_CLASSES[variant]} ${className}`}
    >
      <HelpCircle
        aria-hidden="true"
        className="h-4 w-4"
        strokeWidth={1.75}
      />
    </a>
  );
}
