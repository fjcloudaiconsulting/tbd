/**
 * The ``behavior`` to pass to ``scrollIntoView`` / ``scrollTo``.
 *
 * globals.css sets ``scroll-behavior: auto`` under
 * ``prefers-reduced-motion: reduce``, but an explicit ``behavior: "smooth"``
 * argument overrides the computed property (CSSOM-View), so a script-driven
 * scroll has to choose for itself. Read at call time rather than through a
 * hook: every caller scrolls from an effect or handler, so the value is
 * always current and never needs to sit in a dependency array.
 */
export function scrollBehavior(): ScrollBehavior {
  return typeof window !== "undefined"
    && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches
    ? "auto"
    : "smooth";
}
