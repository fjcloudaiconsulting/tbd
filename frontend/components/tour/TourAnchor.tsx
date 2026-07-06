"use client";

import { cloneElement, isValidElement, type ReactElement, type ReactNode } from "react";

/**
 * TourAnchor — marks an element as a stable target for the first-run
 * tour.
 *
 * This is scaffolding only. The Onboarding team (L3.3, Wave 2) will
 * wire the real tour engine against the `data-tour-id` selectors we
 * register here. In this PR, TourAnchor never adds any visual change.
 *
 * Two usage patterns:
 *
 *   // 1. Inline wrapper (adds a no-op <span> with the data attr).
 *   <TourAnchor id="dashboard.balance-tile">
 *     <BalanceTile />
 *   </TourAnchor>
 *
 *   // 2. Decorate the child directly (preserves DOM shape — preferred
 *   // for layout-sensitive spots).
 *   <TourAnchor id="transactions.filter-bar" as="child">
 *     <div className="filter-bar">...</div>
 *   </TourAnchor>
 *
 * The `as="child"` form clones the single child element and injects
 * `data-tour-id` onto it. Use this when wrapping in an extra <span>
 * would break Tailwind flex/grid expectations.
 *
 * Convention: IDs are dot-namespaced — "<page>.<element>". The
 * Onboarding team can scope tour steps by page prefix.
 */

export interface TourAnchorProps {
  /** Stable tour selector id. Convention: "<page>.<element>". */
  id: string;
  /** Element(s) to anchor. */
  children: ReactNode;
  /**
   * - "wrapper" (default): renders a `<span>` with `data-tour-id`.
   * - "child": clones the single child element and adds `data-tour-id`
   *   to it. Errors if `children` is not a single ReactElement.
   */
  as?: "wrapper" | "child";
}

export default function TourAnchor({
  id,
  children,
  as = "wrapper",
}: TourAnchorProps) {
  if (as === "child") {
    if (!isValidElement(children)) {
      // Fail soft in production; the tour will simply not find this
      // anchor. We log to console in dev to surface mis-usage.
      if (process.env.NODE_ENV !== "production") {
        console.warn(
          `[TourAnchor] as="child" requires a single ReactElement child (id: ${id}).`,
        );
      }
      return <>{children}</>;
    }
    return cloneElement(children as ReactElement<Record<string, unknown>>, {
      "data-tour-id": id,
    } as Record<string, unknown>);
  }

  return (
    <span data-tour-id={id} data-testid="tour-anchor">
      {children}
    </span>
  );
}
