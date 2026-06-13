"use client";

/**
 * Reports v2 — unsaved draft editor (`/reports/new`).
 *
 * A report must NOT exist in the DB until the user explicitly Saves
 * it, even when started from a template. This page opens an in-memory
 * draft (blank starter content, or a template's layout when
 * ``?template=<key>`` is present) and lets the user build it with the
 * same canvas pieces the saved-report editor uses. Save is the ONLY
 * persistence: it POSTs a new report and replaces the URL with the
 * real id so the back button never returns to the throwaway draft.
 *
 * History / Delete / Revert / Duplicate are intentionally absent —
 * there is nothing saved to act on yet. Those live on the saved-report
 * editor at `/reports/[id]`.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";

import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";
import { useFilterChipState } from "@/lib/reports/use-filter-chip-state";
import { createReport, listTemplates } from "@/lib/reports/api";
import { blankDraftSeed } from "@/lib/reports/draft";
import type {
  CanvasFilters,
  LayoutJson,
  Widget,
  WidgetType,
} from "@/lib/reports/types";
import Canvas from "@/components/reports/Canvas";
import CanvasFiltersBar from "@/components/reports/CanvasFiltersBar";
import WidgetEditorPopover from "@/components/reports/WidgetEditorPopover";
import { useWidgetAnchor } from "@/lib/reports/use-widget-anchor";
import WidgetPicker from "@/components/reports/WidgetPicker";
import WidgetShell from "@/components/reports/WidgetShell";
import {
  emptyWidget,
  newWidgetId,
  renderWidgetByType,
} from "@/components/reports/widgetKit";

export default function ReportDraftPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const templateKey = searchParams.get("template");
  const { user, loading: authLoading, featureReportsV2 } = useAuth();

  const [name, setName] = useState("Untitled report");
  const [layout, setLayout] = useState<LayoutJson | null>(null);
  const [canvasFilters, setCanvasFilters] = useState<CanvasFilters>({});
  const [selectedWidgetId, setSelectedWidgetId] = useState<string | null>(null);
  // Shared filter-chip wiring: accounts/categories name lookups (shared
  // SWR cache), the popover Filters-tab deep-link request, and the
  // select-with-Filters chip handler. The draft page is ALWAYS edit mode,
  // so ``selectWidgetFilters`` is always the real handler.
  const {
    accounts,
    categories,
    requestedTab,
    selectWidgetFilters,
    clearRequestedTab,
  } = useFilterChipState(setSelectedWidgetId);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  // Guards the one-time draft seed so re-renders don't clobber edits.
  const seeded = useRef(false);

  useEffect(() => {
    if (authLoading) return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (!featureReportsV2) {
      router.replace("/dashboard");
      return;
    }
    if (seeded.current) return;

    // Seed the in-memory draft. NO DB write happens here — the draft
    // lives in component state until the user clicks Save.
    function seedBlank() {
      const seed = blankDraftSeed();
      setLayout(seed.layout);
      setCanvasFilters(seed.canvasFilters);
      seeded.current = true;
    }

    if (!templateKey) {
      seedBlank();
      return;
    }

    let cancelled = false;
    listTemplates()
      .then((templates) => {
        if (cancelled || seeded.current) return;
        const t = templates.find((x) => x.key === templateKey);
        if (t) {
          setName(t.name);
          setLayout(t.layout_json);
          setCanvasFilters(t.canvas_filters_json ?? {});
          seeded.current = true;
        } else {
          // Unknown/invalid key — fall back to the blank draft.
          seedBlank();
        }
      })
      .catch(() => {
        if (!cancelled && !seeded.current) seedBlank();
      });
    return () => {
      cancelled = true;
    };
    // ``router`` omitted intentionally — useRouter() identity isn't
    // guaranteed stable and refiring would reseed over edits (the
    // ``seeded`` ref also guards that).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user, authLoading, featureReportsV2, templateKey]);

  const selectedWidget = useMemo(
    () => layout?.widgets.find((w) => w.id === selectedWidgetId) ?? null,
    [layout, selectedWidgetId],
  );

  // Resolve the selected widget's shell into the popover anchor. The draft
  // page is ALWAYS in edit mode, so ``enabled`` is hardwired ``true`` (no
  // ``editModeActive`` gate, unlike the saved-report editor).
  const anchorEl = useWidgetAnchor(selectedWidgetId, true, layout?.widgets);

  function updateLayout(next: LayoutJson) {
    setLayout(next);
  }

  // Stable identities so the popover's effects (keyed on ``onClose``) and
  // ``buildWidgetMutations`` don't re-run on every parent render.
  const closePopover = useCallback(() => {
    setSelectedWidgetId(null);
    clearRequestedTab(); // belt-and-suspenders; consume callback is the real clear
  }, [clearRequestedTab]);

  const updateWidget = useCallback((nextWidget: Widget) => {
    setLayout((prev) =>
      prev
        ? {
            ...prev,
            widgets: prev.widgets.map((w) =>
              w.id === nextWidget.id ? nextWidget : w,
            ),
          }
        : prev,
    );
  }, []);

  function addWidget(type: WidgetType) {
    const id = newWidgetId();
    const widget = emptyWidget(type, id);
    setLayout((prev) => {
      const widgets = prev?.widgets ?? [];
      const maxY = widgets.reduce(
        (max, w) => Math.max(max, w.grid.y + w.grid.h),
        0,
      );
      widget.grid = { ...widget.grid, x: 0, y: maxY };
      return { version: 1, widgets: [...widgets, widget] };
    });
    setSelectedWidgetId(id);
    setPickerOpen(false);
  }

  function removeWidget(widgetId: string) {
    setLayout((prev) =>
      prev
        ? { ...prev, widgets: prev.widgets.filter((w) => w.id !== widgetId) }
        : prev,
    );
    if (selectedWidgetId === widgetId) setSelectedWidgetId(null);
  }

  async function handleSave() {
    if (!layout || saving) return;
    setSaving(true);
    setSaveError(null);
    try {
      const created = await createReport({
        name: name.trim() || "Untitled report",
        visibility: "private",
        layout_json: layout,
        canvas_filters_json: canvasFilters,
      });
      // Replace (not push) so the back button skips the throwaway draft.
      router.replace(`/reports/${created.id}`);
    } catch (err) {
      const e = err as Error;
      setSaveError(e.message || "Couldn't save report");
      setSaving(false);
    }
  }

  function handleCancel() {
    // Discard the in-memory draft — nothing was persisted.
    router.push("/reports");
  }

  if (authLoading || !layout) {
    return (
      <AppShell>
        <div className="flex h-full items-center justify-center">
          <div
            data-testid="report-draft-loading"
            className="h-6 w-6 animate-spin rounded-full border-2 border-border border-t-accent"
          />
        </div>
      </AppShell>
    );
  }

  return (
    <AppShell>
      <div className="flex h-full flex-col" data-testid="report-draft">
        <header className="flex items-center justify-between border-b border-border bg-surface px-4 py-3">
          <div className="flex items-center gap-3">
            <Link
              href="/reports"
              className="text-sm text-text-muted hover:text-text-primary"
            >
              Reports
            </Link>
            <span className="text-text-muted">/</span>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              aria-label="Report name"
              data-testid="report-draft-name"
              className="rounded-md border border-border bg-bg px-2 py-1 text-sm font-semibold text-text-primary"
            />
            <span
              data-testid="report-draft-unsaved"
              className="text-xs text-text-muted"
            >
              Draft (not saved)
            </span>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setPickerOpen(true)}
              className="rounded-md border border-border px-3 py-1.5 text-sm text-text-primary hover:bg-surface-raised"
              data-testid="report-draft-add-widget"
            >
              Add widget
            </button>
            <button
              type="button"
              onClick={handleSave}
              disabled={saving}
              className="rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-accent-text transition hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-60"
              data-testid="report-draft-save"
            >
              {saving ? "Saving..." : "Save"}
            </button>
            <button
              type="button"
              onClick={handleCancel}
              className="rounded-md border border-border px-3 py-1.5 text-sm text-text-primary hover:bg-surface-raised"
              data-testid="report-draft-cancel"
            >
              Cancel
            </button>
          </div>
        </header>

        {saveError && (
          <div
            role="alert"
            className="border-b border-danger/30 bg-danger/10 px-4 py-2 text-sm text-danger"
          >
            {saveError}
          </div>
        )}

        <div className="border-b border-border bg-bg px-4 py-3">
          <CanvasFiltersBar value={canvasFilters} onChange={setCanvasFilters} />
        </div>

        <div className="flex flex-1 overflow-hidden">
          <div
            data-testid="report-canvas-column"
            className="flex-1 overflow-y-auto px-4 py-4"
          >
            {layout.widgets.length === 0 ? (
              <div
                data-testid="report-draft-empty"
                className="rounded-md border border-dashed border-border bg-surface px-6 py-10 text-center"
              >
                <p className="text-sm font-medium text-text-primary">
                  This draft has no widgets yet
                </p>
                <p className="mx-auto mt-1 max-w-md text-sm text-text-muted">
                  Add a widget to see your data. The canvas date applies to
                  every widget; accounts and categories are per-widget.
                </p>
                <div className="mt-4 flex items-center justify-center">
                  <button
                    type="button"
                    onClick={() => setPickerOpen(true)}
                    className="rounded-md bg-accent px-4 py-2 text-sm font-semibold text-accent-text transition hover:bg-accent-hover"
                    data-testid="report-draft-empty-add-widget"
                  >
                    Add widget
                  </button>
                </div>
              </div>
            ) : (
              <Canvas
                layout={layout}
                editMode
                onLayoutChange={updateLayout}
                renderWidget={(w) => (
                  <WidgetShell
                    widgetId={w.id}
                    selected={selectedWidgetId === w.id}
                    editMode
                    onSelect={() => setSelectedWidgetId(w.id)}
                    onRemove={() => removeWidget(w.id)}
                    widget={w}
                    canvasFilters={canvasFilters}
                    accounts={accounts}
                    categories={categories}
                    // Always edit mode here → the chip's real handler.
                    onSelectFilters={() => selectWidgetFilters(w.id)}
                  >
                    {renderWidgetByType(w, canvasFilters, true)}
                  </WidgetShell>
                )}
              />
            )}
          </div>
        </div>

        {/* Widget editor — anchored floating popover portaled to the body,
            never a flex sibling of the canvas, so the canvas does not reflow
            when a widget is selected. The draft page is always in edit mode,
            so the gate is ``selectedWidget && anchorEl`` only. */}
        {selectedWidget && anchorEl && (
          <WidgetEditorPopover
            widget={selectedWidget}
            canvasFilters={canvasFilters}
            anchorEl={anchorEl}
            requestedTab={requestedTab ?? undefined}
            onTabConsumed={clearRequestedTab}
            onUpdate={updateWidget}
            onClose={closePopover}
          />
        )}

        <WidgetPicker
          open={pickerOpen}
          onClose={() => setPickerOpen(false)}
          onPick={addWidget}
        />
      </div>
    </AppShell>
  );
}
