"use client";

import { useDndMonitor, useDroppable } from "@dnd-kit/core";
import type { ReactNode } from "react";
import { useState } from "react";

/**
 * Drop target for a master category card. Highlights when a compatible
 * subcategory is being dragged over it. Cross-type drops render a
 * disabled state instead of an accent highlight, and dropping does
 * nothing (the page-level `onDragEnd` short-circuits the move).
 *
 * Owned by Team Categories C2 UI (C2b).
 */
interface Props {
  masterId: number;
  masterType: "income" | "expense" | "both";
  enabled: boolean;
  children: ReactNode;
}

interface ActiveDragData {
  kind?: string;
  subcategoryType?: "income" | "expense" | "both";
  parentId?: number;
}

export default function MasterDropZone({
  masterId,
  masterType,
  enabled,
  children,
}: Props) {
  const { setNodeRef, isOver, active } = useDroppable({
    id: `master-${masterId}`,
    disabled: !enabled,
    data: {
      kind: "master",
      masterId,
      masterType,
    },
  });

  // `active.data.current` may be undefined depending on the activation
  // path; `useDndMonitor` keeps a stable mirror updated on every drag
  // tick which is more robust during keyboard / programmatic drags.
  const [activeData, setActiveData] = useState<ActiveDragData | null>(null);
  useDndMonitor({
    onDragStart(event) {
      const data = event.active.data.current as ActiveDragData | undefined;
      setActiveData(data ?? null);
    },
    onDragEnd() {
      setActiveData(null);
    },
    onDragCancel() {
      setActiveData(null);
    },
  });

  const dragging = active != null && enabled;
  const dragData = activeData
    ?? ((active?.data.current as ActiveDragData | undefined) ?? null);

  const sameType = dragData?.kind === "subcategory"
    && dragData.subcategoryType === masterType;
  const isSourceParent = dragData?.kind === "subcategory"
    && dragData.parentId === masterId;

  // Drop state classification:
  //   - source-parent: not a valid drop target (no-op), neutral state.
  //   - cross-type: shown as disabled even when hovered.
  //   - same-type: hover shows the accent-dim highlight.
  const showAsValidTarget = dragging && sameType && !isSourceParent;
  const showAsInvalidHover = dragging && isOver && !sameType;
  const showAsSourceParentHover = dragging && isOver && isSourceParent;

  let dropStateClass = "";
  if (showAsInvalidHover) {
    dropStateClass = "ring-2 ring-danger/40 cursor-not-allowed";
  } else if (showAsValidTarget && isOver) {
    dropStateClass = "ring-2 ring-accent bg-accent-dim";
  } else if (showAsValidTarget) {
    dropStateClass = "ring-1 ring-accent/40";
  } else if (showAsSourceParentHover) {
    // Hovering over the current parent — neutral, no API call will fire.
    dropStateClass = "ring-1 ring-border";
  }

  return (
    <div
      ref={setNodeRef}
      data-testid={`master-dropzone-${masterId}`}
      data-drop-valid={showAsValidTarget ? "true" : "false"}
      data-drop-invalid={showAsInvalidHover ? "true" : "false"}
      className={`transition-shadow ${dropStateClass}`}
    >
      {children}
    </div>
  );
}
