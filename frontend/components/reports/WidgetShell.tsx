"use client";

/**
 * Visual chrome around each canvas widget — drag handle (when in
 * edit mode), title, error boundary surface, click-to-select. The
 * widget itself renders inside ``children``.
 *
 * react-grid-layout requires the drag-handle class to live on a
 * specific element; we expose it via the ``data-grid-drag-handle``
 * attribute so the canvas wrapper can hook it without leaking the
 * library detail into widget components.
 */
import { ReactNode } from "react";
import { GripVertical, X } from "lucide-react";

interface Props {
  widgetId: string;
  selected: boolean;
  editMode: boolean;
  onSelect: () => void;
  onRemove?: () => void;
  children: ReactNode;
}

export default function WidgetShell({
  widgetId,
  selected,
  editMode,
  onSelect,
  onRemove,
  children,
}: Props) {
  return (
    <div
      data-widget-shell={widgetId}
      data-selected={selected}
      onClick={onSelect}
      className={`relative flex h-full flex-col rounded-lg ${
        selected ? "ring-2 ring-accent" : ""
      }`}
    >
      {editMode && (
        <div className="absolute right-1 top-1 z-10 flex items-center gap-1">
          <span
            className="cursor-grab rounded p-1 text-text-muted hover:bg-surface hover:text-text-primary"
            data-grid-drag-handle
            aria-label="Drag widget"
          >
            <GripVertical aria-hidden="true" className="h-3.5 w-3.5" />
          </span>
          {onRemove && (
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onRemove();
              }}
              className="rounded p-1 text-text-muted hover:bg-danger/10 hover:text-danger"
              aria-label="Remove widget"
            >
              <X aria-hidden="true" className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
      )}
      <div className="h-full w-full">{children}</div>
    </div>
  );
}
