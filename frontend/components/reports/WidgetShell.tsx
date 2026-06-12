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

import WidgetFilterChips from "@/components/reports/WidgetFilterChips";
import type { CanvasFilters, Widget } from "@/lib/reports/types";
import type { Account, Category } from "@/lib/types";

interface Props {
  widgetId: string;
  selected: boolean;
  editMode: boolean;
  onSelect: () => void;
  onRemove?: () => void;
  /** Widget + canvas + lookups feed the effective filter-chip header. */
  widget: Widget;
  canvasFilters: CanvasFilters;
  accounts: Account[];
  categories: Category[];
  /** Select the widget + open the popover's Filters tab (chip click). */
  onSelectFilters: () => void;
  children: ReactNode;
}

export default function WidgetShell({
  widgetId,
  selected,
  editMode,
  onSelect,
  onRemove,
  widget,
  canvasFilters,
  accounts,
  categories,
  onSelectFilters,
  children,
}: Props) {
  return (
    <div
      data-widget-shell={widgetId}
      data-selected={selected}
      onClick={onSelect}
      // The selected widget anchors the widget-editor dialog (popover);
      // announce that relationship for assistive tech. Known limitation:
      // this is a div, not a button, so the haspopup/expanded contract is
      // best-effort — consistent with the existing click-to-select pattern.
      aria-haspopup="dialog"
      aria-expanded={selected}
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
      {/* Effective filter-chip header — visible in both view and edit
          mode (status-is-data informational). Left-aligned so it never
          collides with the absolutely-positioned edit overlay top-right.
          Renders nothing when the widget has no set filters. */}
      <WidgetFilterChips
        widget={widget}
        canvasFilters={canvasFilters}
        accounts={accounts}
        categories={categories}
        onSelectFilters={onSelectFilters}
      />
      <div className="min-h-0 w-full flex-1">{children}</div>
    </div>
  );
}
