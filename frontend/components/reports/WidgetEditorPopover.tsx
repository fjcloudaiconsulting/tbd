"use client";

/**
 * Anchored floating widget-editor popover. Floats over the canvas,
 * anchored to the selected widget's shell DOM node (``anchorEl``), so it
 * never consumes flex width and the canvas does not reflow when a widget
 * is selected. It owns positioning (``useFloating`` + ``autoUpdate``),
 * dismissal (``useDismiss`` outside-press + Escape), focus management
 * (``FloatingFocusManager``), the role/aria contract (``useRole``), tab
 * state, and which tabs/sub-controls a widget type shows. Every *mutation*
 * is delegated to the extracted control components (``DataTab`` /
 * ``StyleTab`` / ``FilterEditor`` via ``buildWidgetMutations``).
 */
import { useEffect, useId, useState } from "react";
import {
  useFloating,
  autoUpdate,
  offset,
  flip,
  shift,
  useDismiss,
  useRole,
  useInteractions,
  FloatingPortal,
  FloatingFocusManager,
} from "@floating-ui/react";

import DataTab from "@/components/reports/config/DataTab";
import StyleTab from "@/components/reports/config/StyleTab";
import FilterEditor from "@/components/reports/config/FilterEditor";
import { buildWidgetMutations } from "@/components/reports/config/useWidgetMutations";
import type { CanvasFilters, Widget } from "@/lib/reports/types";

interface Props {
  widget: Widget;
  canvasFilters: CanvasFilters;
  anchorEl: HTMLElement | null;
  onUpdate: (next: Widget) => void;
  onClose: () => void;
}

type TabKey = "data" | "filters" | "style";

export default function WidgetEditorPopover({
  widget,
  canvasFilters,
  anchorEl,
  onUpdate,
  onClose,
}: Props) {
  const [tab, setTab] = useState<TabKey>("data");
  const baseId = useId();

  const { refs, floatingStyles, context } = useFloating({
    open: true,
    onOpenChange: (next) => {
      if (!next) onClose();
    },
    placement: "right-start",
    middleware: [offset(8), flip({ padding: 8 }), shift({ padding: 8 })],
    whileElementsMounted: autoUpdate,
  });

  // Anchor to the selected widget element (external reference element).
  useEffect(() => {
    refs.setReference(anchorEl);
  }, [anchorEl, refs]);

  // querySelector anchor staleness guard: if the resolved node detaches
  // (widget removed / canvas re-rendered the shell), close (deselect).
  useEffect(() => {
    if (anchorEl && !anchorEl.isConnected) onClose();
  }, [anchorEl, onClose]);

  // Reset to the default tab when the selected widget identity changes,
  // so opening a different widget never lands on a stale tab.
  useEffect(() => {
    setTab("data");
  }, [widget.id]);

  const dismiss = useDismiss(context, {
    outsidePress: true,
    escapeKey: true,
  });
  const role = useRole(context, { role: "dialog" });
  const { getFloatingProps } = useInteractions([dismiss, role]);

  const { setFilters } = buildWidgetMutations(widget, onUpdate);

  if (!anchorEl) return null;

  const tabs: Array<{ key: TabKey; label: string }> = [
    { key: "data", label: "Data" },
    { key: "filters", label: "Filters" },
    { key: "style", label: "Style" },
  ];

  return (
    <FloatingPortal>
      <FloatingFocusManager context={context} modal={false} initialFocus={-1}>
        <div
          // react-hooks/refs flags reading `refs.setFloating` in render,
          // but floating-ui's `setFloating` is a documented ref-SETTER
          // callback meant to be passed straight to `ref=` (not a
          // `.current` read), so the rule is a false positive here.
          // eslint-disable-next-line react-hooks/refs
          ref={refs.setFloating}
          style={floatingStyles}
          {...getFloatingProps()}
          data-testid="widget-editor-popover"
          aria-label="Widget settings"
          className="z-50 flex max-h-[80vh] w-80 flex-col overflow-hidden rounded-lg border border-border bg-surface shadow-lg"
        >
          <div className="flex items-center justify-between border-b border-border p-4">
            <h2 className="text-sm font-semibold text-text-primary">
              Widget settings
            </h2>
            <button
              type="button"
              onClick={onClose}
              className="text-xs text-text-muted hover:text-text-primary"
            >
              Close
            </button>
          </div>

          <div role="tablist" aria-label="Widget settings tabs" className="flex border-b border-border">
            {tabs.map((t, i) => {
              const active = tab === t.key;
              return (
                <button
                  key={t.key}
                  type="button"
                  role="tab"
                  id={`${baseId}-tab-${t.key}`}
                  aria-selected={active}
                  aria-controls={`${baseId}-panel-${t.key}`}
                  tabIndex={active ? 0 : -1}
                  onClick={() => setTab(t.key)}
                  onKeyDown={(e) => {
                    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
                    e.preventDefault();
                    const delta = e.key === "ArrowRight" ? 1 : -1;
                    const nextIndex = (i + delta + tabs.length) % tabs.length;
                    const next = tabs[nextIndex];
                    setTab(next.key);
                    document
                      .getElementById(`${baseId}-tab-${next.key}`)
                      ?.focus();
                  }}
                  className={
                    active
                      ? "flex-1 border-b-2 border-accent px-3 py-2 text-xs font-medium text-text-primary"
                      : "flex-1 border-b-2 border-transparent px-3 py-2 text-xs font-medium text-text-muted hover:text-text-primary"
                  }
                >
                  {t.label}
                </button>
              );
            })}
          </div>

          <div className="flex flex-col gap-4 overflow-y-auto p-4">
            <div
              role="tabpanel"
              id={`${baseId}-panel-data`}
              aria-labelledby={`${baseId}-tab-data`}
              hidden={tab !== "data"}
              className="flex flex-col gap-4"
            >
              {tab === "data" && <DataTab widget={widget} onUpdate={onUpdate} />}
            </div>
            <div
              role="tabpanel"
              id={`${baseId}-panel-filters`}
              aria-labelledby={`${baseId}-tab-filters`}
              hidden={tab !== "filters"}
              className="flex flex-col gap-4"
            >
              {tab === "filters" && (
                <FilterEditor
                  filters={widget.config.filters ?? {}}
                  canvasFilters={canvasFilters}
                  onChange={setFilters}
                />
              )}
            </div>
            <div
              role="tabpanel"
              id={`${baseId}-panel-style`}
              aria-labelledby={`${baseId}-tab-style`}
              hidden={tab !== "style"}
              className="flex flex-col gap-4"
            >
              {tab === "style" && <StyleTab widget={widget} onUpdate={onUpdate} />}
            </div>
          </div>
        </div>
      </FloatingFocusManager>
    </FloatingPortal>
  );
}
