"use client";

import { Children, isValidElement, ReactElement, ReactNode } from "react";

/**
 * Bottom-right floating-widget anchor.
 *
 * Stacking convention (locked for the FAB + future feedback widget):
 *   - One cluster at the bottom-right corner. NOT two separate zones.
 *   - Children render in a vertical column, gap-3, items-end.
 *   - "primary" slot pins to the bottom (closest to the user's thumb on
 *     mobile). The Add Transaction FAB owns the primary slot.
 *   - "secondary" slot stacks above primary. The future in-app feedback
 *     widget will own this slot.
 *   - "tertiary" exists for one further widget if we ever need it.
 *
 * Adding a new floating widget is a one-component-tree edit: drop a new
 * <AnchorZoneSlot slot="secondary"> child anywhere in the tree that the
 * AnchorZone wraps. The cluster sorts children by slot priority so the
 * vertical order stays stable regardless of mount order.
 *
 * z-index: 40. Sits below modal overlays (z-50, e.g. ConfirmModal +
 * SlideInPanel which both use z-50) so an open dialog dims the FAB.
 */

export type AnchorSlot = "primary" | "secondary" | "tertiary";

const SLOT_ORDER: Record<AnchorSlot, number> = {
  // Lower number = lower in the visual stack (closer to corner).
  primary: 0,
  secondary: 1,
  tertiary: 2,
};

interface SlotProps {
  slot: AnchorSlot;
  children: ReactNode;
}

/**
 * Wrapper to opt a child into a specific slot. The AnchorZone sorts its
 * children by slot priority so consumers don't have to reason about
 * mount order across pages.
 */
export function AnchorZoneSlot({ children }: SlotProps) {
  return <>{children}</>;
}
AnchorZoneSlot.displayName = "AnchorZoneSlot";

interface AnchorZoneProps {
  children: ReactNode;
  /**
   * Optional test id for the cluster wrapper. Mainly used by tests that
   * need to assert ordering or z-index without a stable visible label.
   */
  testId?: string;
}

interface RankedChild {
  rank: number;
  index: number;
  node: ReactNode;
}

function rankChild(child: ReactNode, index: number): RankedChild {
  if (
    isValidElement(child) &&
    (child.type as { displayName?: string }).displayName === "AnchorZoneSlot"
  ) {
    const slot = (child.props as SlotProps).slot;
    return { rank: SLOT_ORDER[slot] ?? 99, index, node: child };
  }
  // Bare children get a high rank so they stack above named slots only
  // by accident. Prefer wrapping in <AnchorZoneSlot> for predictable
  // ordering. The fallback keeps AnchorZone tolerant of plain children.
  return { rank: 99, index, node: child };
}

export default function AnchorZone({ children, testId }: AnchorZoneProps) {
  const ranked = Children.toArray(children)
    .map(rankChild)
    // Visually we want primary (rank 0) at the BOTTOM. Flex-col-reverse
    // would invert tab order, so we sort descending instead and keep the
    // natural column. Tab order: secondary first, then primary, matches
    // the visual top-to-bottom flow.
    .sort((a, b) => {
      if (b.rank !== a.rank) return b.rank - a.rank;
      return a.index - b.index;
    });

  return (
    <div
      data-testid={testId ?? "anchor-zone"}
      className="pointer-events-none fixed bottom-4 right-4 z-40 flex flex-col items-end gap-3 sm:bottom-6 sm:right-6"
    >
      {ranked.map((c, i) => (
        <div key={i} className="pointer-events-auto">
          {c.node}
        </div>
      ))}
    </div>
  );
}
