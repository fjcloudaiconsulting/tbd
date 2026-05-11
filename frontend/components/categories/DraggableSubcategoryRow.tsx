"use client";

import { useDraggable } from "@dnd-kit/core";
import type { ReactNode } from "react";
import { GripVertical } from "lucide-react";

/**
 * Drag source for a subcategory row. The drag handle is rendered to the
 * left of the row content and is the ONLY drag activator (using
 * dnd-kit's `listeners` on the handle keeps the row contents clickable —
 * checkbox, Edit/Delete buttons, the row body etc.).
 *
 * Owned by Team Categories C2 UI (C2b).
 */
interface Props {
  subcategoryId: number;
  subcategoryName: string;
  subcategoryType: "income" | "expense" | "both";
  parentId: number;
  enabled: boolean;
  children: ReactNode;
}

export default function DraggableSubcategoryRow({
  subcategoryId,
  subcategoryName,
  subcategoryType,
  parentId,
  enabled,
  children,
}: Props) {
  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({
    id: `sub-${subcategoryId}`,
    disabled: !enabled,
    data: {
      kind: "subcategory",
      subcategoryId,
      subcategoryName,
      subcategoryType,
      parentId,
    },
  });

  return (
    <div
      ref={setNodeRef}
      data-testid={`sub-draggable-${subcategoryId}`}
      data-dragging={isDragging ? "true" : "false"}
      className={`flex items-center gap-1 ${
        isDragging ? "opacity-50" : ""
      }`}
    >
      {enabled && (
        <button
          type="button"
          data-testid={`sub-drag-handle-${subcategoryId}`}
          aria-label={`Drag ${subcategoryName}`}
          className="flex min-h-[44px] min-w-[44px] cursor-grab touch-none items-center justify-center text-text-muted hover:text-accent active:cursor-grabbing md:min-h-8 md:min-w-6"
          {...attributes}
          {...listeners}
        >
          <GripVertical className="h-4 w-4" aria-hidden="true" />
        </button>
      )}
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
}
