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
 */
type HelpAnchorProps = {
  /** /docs section id, e.g. "dashboard". Must exist in the docs page. */
  section: string;
  /** Optional extra context for screen readers. Falls back to the section name. */
  label?: string;
  /** Optional class overrides for layout fit (margin, alignment). */
  className?: string;
};

export default function HelpAnchor({
  section,
  label,
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
      className={`inline-flex min-h-[44px] min-w-[44px] md:min-h-7 md:min-w-7 items-center justify-center rounded-full text-text-muted hover:text-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30 ${className}`}
    >
      <HelpCircle
        aria-hidden="true"
        className="h-4 w-4"
        strokeWidth={1.75}
      />
    </a>
  );
}
