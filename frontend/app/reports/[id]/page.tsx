"use client";

/**
 * Reports v2 — single-report editor.
 *
 * Loads the report by id, hydrates the canvas + widgets, lets the
 * user add / configure / resize / drag widgets, and saves the
 * layout JSON via PATCH on explicit "Save" click.
 *
 * Save semantics (PR2 decision): explicit save button, NOT
 * debounced auto-save. Reasoning:
 *  - explicit save keeps every PATCH a user-initiated act and
 *    keeps "dirty" state visible (Save → Saved transition).
 *  - debounced auto-save tangles with the SWR query layer per
 *    widget (rapid filter changes would churn the layout PATCH
 *    AND fire 8 query refetches per debounce slot).
 *  - explicit save fits PR2 scope; PR4 can layer auto-save on
 *    top once sharing + templates land.
 *
 * Architect ambiguity resolved: spec §1 ASCII mockup shows a "Save"
 * button in the header; the rollout-table PR2 row says "Save layout
 * (PATCH)" — both align with explicit-save.
 */
import { TriangleAlert } from "lucide-react";
import { use, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";

import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";
import { useFilterChipState } from "@/lib/reports/use-filter-chip-state";
import {
  deleteReport,
  duplicateReport,
  getReport,
  restoreVersion,
  saveLayout,
  updateReport,
} from "@/lib/reports/api";
import type {
  CanvasFilters,
  LayoutJson,
  ReportSummary,
  ReportVersionSummary,
  Widget,
} from "@/lib/reports/types";
import ConfirmModal from "@/components/ui/ConfirmModal";
import Canvas from "@/components/reports/Canvas";
import HistoryPanel from "@/components/reports/HistoryPanel";
import CanvasFiltersBar from "@/components/reports/CanvasFiltersBar";
import WidgetEditorPopover from "@/components/reports/WidgetEditorPopover";
import { useWidgetAnchor } from "@/lib/reports/use-widget-anchor";
import WidgetPicker from "@/components/reports/WidgetPicker";
import WidgetShell from "@/components/reports/WidgetShell";
import { renderReportWidget } from "@/components/reports/renderReportWidget";
import { reportCurrency } from "@/lib/reports/series";
import { mobileStackHeight, orderWidgetsForStack } from "@/lib/reports/stack";
import { emptyWidget, newWidgetId } from "@/components/reports/widgetKit";
import type { WidgetType } from "@/lib/reports/types";
import { useIsMobile } from "@/lib/hooks/use-is-mobile";
import { btnCanvas, btnCanvasActive } from "@/lib/styles";
import { useReportSources } from "@/lib/reports/use-report-sources";

interface PageProps {
  // Next 15 makes ``params`` a promise on server-rendered pages; in
  // tests we can also pass a plain object so we don't have to wrap
  // every render in a Suspense boundary that handles ``use()``.
  params: Promise<{ id: string }> | { id: string };
}

const DEFAULT_LAYOUT: LayoutJson = { version: 1, widgets: [] };

/**
 * ⚠ TBD-382 R13: this module used to carry its OWN byte-identical copies of
 * `emptyKPI` / `emptyBar` / `emptyMultiSeries` / `emptyWidget` / `newWidgetId`,
 * and they were LIVE — the saved-report editor called the local copies while
 * `/reports/new` called widgetKit's. Any change to a widget seed applied to
 * one module only meant the two editors created different widgets. There is
 * now exactly one factory; do not reintroduce a local copy.
 */

export default function ReportEditorPage({ params }: PageProps) {
  // Next 15 makes ``params`` a promise; ``use()`` unwraps it on the
  // client without awaiting at the top level. In test harnesses we
  // can pass a plain object and skip the promise branch entirely so
  // the editor doesn't suspend.
  const resolvedParams =
    params && typeof (params as Promise<{ id: string }>).then === "function"
      ? use(params as Promise<{ id: string }>)
      : (params as { id: string });
  const { id } = resolvedParams;
  const router = useRouter();
  const { user, loading: authLoading, features } = useAuth();
  const isSmallScreen = useIsMobile();

  const [report, setReport] = useState<ReportSummary | null>(null);
  // Draft value of the inline-editable title. Seeded from the loaded
  // report and re-synced whenever the report identity changes (load,
  // restore, duplicate-in-place) — NOT on every ``report`` write, so a
  // mid-typing PATCH response can't clobber what the user is typing.
  const [titleDraft, setTitleDraft] = useState("");
  // Synchronous re-entry guard so Enter-then-blur in the same tick can't
  // fire two PATCHes — a state flag would flip a tick too late, so we use a ref.
  const titleCommitInFlight = useRef(false);
  const [layout, setLayout] = useState<LayoutJson>(DEFAULT_LAYOUT);
  const [canvasFilters, setCanvasFilters] = useState<CanvasFilters>({});
  const [selectedWidgetId, setSelectedWidgetId] = useState<string | null>(null);
  // Shared filter-chip wiring: accounts/categories name lookups (shared
  // SWR cache), the popover Filters-tab deep-link request, and the
  // select-with-Filters chip handler. ``setSelectedWidgetId`` is a stable
  // useState setter so the hook's callbacks keep a stable identity.
  const {
    accounts,
    categories,
    requestedTab,
    selectWidgetFilters,
    clearRequestedTab,
  } = useFilterChipState(setSelectedWidgetId);
  // Reports are single-currency in practice (cross-currency mixing is not
  // done), so derive one display currency from the org's accounts and
  // thread it into every widget for currency-symbol formatting.
  const currency = reportCurrency(accounts);
  const [editMode, setEditMode] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  // TBD-430: surfaced, not swallowed. `sources: []` is BOTH "empty
  // catalog" and (until TBD-403 part 1) "catalog is down"; only the SWR
  // error separates them, and only the failure warrants a banner.
  const { error: sourcesError } = useReportSources();

  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [lastSavedAt, setLastSavedAt] = useState<Date | null>(null);
  const [showSavedToast, setShowSavedToast] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [duplicating, setDuplicating] = useState(false);
  const [togglingVisibility, setTogglingVisibility] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [pendingRestore, setPendingRestore] =
    useState<ReportVersionSummary | null>(null);

  // Hydrate the canvas state (report snapshot + layout + filters) from
  // a server ``ReportSummary``. Reused by the initial load, Cancel
  // (restore the last-saved snapshot), and Revert (rolled-back
  // snapshot).
  function hydrateFromReport(r: ReportSummary) {
    setReport(r);
    setTitleDraft(r.name);
    const lj = (r.layout_json ?? {}) as Partial<LayoutJson>;
    setLayout(
      lj && Array.isArray(lj.widgets)
        ? { version: 1, widgets: lj.widgets as Widget[] }
        : DEFAULT_LAYOUT,
    );
    setCanvasFilters((r.canvas_filters_json as CanvasFilters) ?? {});
    setSelectedWidgetId(null);
    setDirty(false);
  }

  // Owner-only affordances. The backend enforces this regardless, but
  // hiding Delete/Revert for non-owners keeps the header honest when
  // the page already knows the viewer isn't the owner.
  const canEdit = !!user && !!report && report.owner_user_id === user.id;

  useEffect(() => {
    if (authLoading) return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (features?.reports === false) {
      router.replace("/dashboard");
      return;
    }
    let cancelled = false;
    getReport(Number(id))
      .then((r) => {
        if (cancelled) return;
        hydrateFromReport(r);
        // Default to view mode for reports that already have content; a
        // blank (0-widget) report opens in edit mode so the user has an
        // obvious starting point.
        const lj = (r.layout_json ?? {}) as Partial<LayoutJson>;
        const widgetCount = Array.isArray(lj.widgets) ? lj.widgets.length : 0;
        setEditMode(widgetCount === 0);
      })
      .catch((err: Error) => {
        if (!cancelled) setLoadError(err.message || "Couldn't load report");
      });
    return () => {
      cancelled = true;
    };
    // ``router`` is intentionally omitted — useRouter() may not
    // guarantee a referentially-stable identity across renders, and
    // refiring the load effect would clobber unsaved canvas edits.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, user, authLoading, features]);

  // Auto-dismiss the "Report saved" toast ~2s after it appears.
  useEffect(() => {
    if (!showSavedToast) return;
    const t = setTimeout(() => setShowSavedToast(false), 2000);
    return () => clearTimeout(t);
  }, [showSavedToast]);

  const selectedWidget = useMemo(
    () => layout.widgets.find((w) => w.id === selectedWidgetId) ?? null,
    [layout.widgets, selectedWidgetId],
  );

  // Editing is desktop-only AND owner-only. On small screens (< sm) we
  // force VIEW mode regardless of the user's ``editMode`` toggle: the
  // report renders as a read-only single-column stack with no drag/resize
  // and no edit toolbar. Non-owners (e.g. viewers of an org-shared
  // report) never enter edit mode either — the backend 403s their
  // PATCH/DELETE/restore, so surfacing edit chrome would only let them
  // build unsavable local changes. ``editMode`` is preserved in state so
  // resizing back up to desktop (as an owner) restores what they had open.
  const editModeActive = editMode && !isSmallScreen && canEdit;

  // Resolve the selected widget's shell into the popover anchor. Edit here
  // is gated on ``editModeActive`` (desktop + owner + edit toggle).
  const anchorEl = useWidgetAnchor(
    selectedWidgetId,
    editModeActive,
    layout.widgets,
  );

  function updateLayout(next: LayoutJson) {
    setLayout(next);
    setDirty(true);
  }

  // Stable identities so the popover's effects (keyed on ``onClose``) and
  // ``buildWidgetMutations`` don't re-run on every parent render.
  const closePopover = useCallback(() => {
    setSelectedWidgetId(null);
    // Belt-and-suspenders: the consume callback is the real clear, but
    // also drop any pending request on close so a stale chip request
    // can't leak into the next selection.
    clearRequestedTab();
  }, [clearRequestedTab]);

  const updateWidget = useCallback((nextWidget: Widget) => {
    setLayout((prev) => ({
      ...prev,
      widgets: prev.widgets.map((w) =>
        w.id === nextWidget.id ? nextWidget : w,
      ),
    }));
    setDirty(true);
  }, []);

  function addWidget(type: WidgetType) {
    const id = newWidgetId();
    const widget = emptyWidget(type, id);
    // Drop the new widget at the bottom of the existing stack.
    const maxY = layout.widgets.reduce(
      (max, w) => Math.max(max, w.grid.y + w.grid.h),
      0,
    );
    widget.grid = { ...widget.grid, x: 0, y: maxY };
    setLayout((prev) => ({ ...prev, widgets: [...prev.widgets, widget] }));
    setSelectedWidgetId(id);
    setPickerOpen(false);
    setDirty(true);
  }

  function removeWidget(widgetId: string) {
    setLayout((prev) => ({
      ...prev,
      widgets: prev.widgets.filter((w) => w.id !== widgetId),
    }));
    if (selectedWidgetId === widgetId) setSelectedWidgetId(null);
    setDirty(true);
  }

  function updateCanvasFilters(next: CanvasFilters) {
    setCanvasFilters(next);
    setDirty(true);
  }

  // Inline title rename. Independent of the layout dirty/Save flow: a
  // rename is its own immediate PATCH (the backend treats a name change
  // as a non-snapshotting metadata edit). Commit on blur AND Enter; if
  // the trimmed value is empty or unchanged, do nothing (revert blank
  // back to the current name). On error, revert the draft to the prior
  // name so the input never strands an unpersisted value.
  async function commitTitle() {
    if (!report || titleCommitInFlight.current) return;
    const trimmed = titleDraft.trim();
    if (!trimmed) {
      setTitleDraft(report.name);
      return;
    }
    if (trimmed === report.name) {
      // Unchanged once trimmed: normalize the visible draft back to the
      // canonical name so surrounding whitespace the user typed (e.g.
      // "My Report ") doesn't linger in the input.
      setTitleDraft(report.name);
      return;
    }
    const prevName = report.name;
    titleCommitInFlight.current = true;
    try {
      const updated = await updateReport(report.id, { name: trimmed });
      setReport(updated);
      setTitleDraft(updated.name);
    } catch (err) {
      setTitleDraft(prevName);
      const e = err as Error;
      setSaveError(e.message || "Couldn't rename report");
    } finally {
      titleCommitInFlight.current = false;
    }
  }

  async function handleSave() {
    if (!report || saving) return;
    setSaving(true);
    setSaveError(null);
    try {
      const saved = await saveLayout(report.id, layout, canvasFilters);
      setReport(saved);
      setDirty(false);
      setLastSavedAt(new Date());
      setShowSavedToast(true);
      // Post-save presentation: land the user on a read-only view of the
      // just-saved report (charts render, no drag handles / edit chrome)
      // rather than leaving them in the builder or navigating to the list.
      // ``report`` now holds the saved snapshot, so the view renders the
      // persisted layout; the "Edit" toggle re-enters the builder.
      setSelectedWidgetId(null);
      setEditMode(false);
    } catch (err) {
      const e = err as Error;
      setSaveError(e.message || "Couldn't save layout");
    } finally {
      setSaving(false);
    }
  }

  // Cancel editing — discard unsaved local changes by restoring the
  // last-saved server snapshot the page already holds in ``report``,
  // then drop back to view mode. No network call: ``report`` always
  // reflects the most recent successful load/save.
  function handleCancelEdit() {
    if (report) hydrateFromReport(report);
    // Mirror handleSave: clear any open selection + pending tab request so
    // dropping to view mode never leaves a selection ring with no popover.
    setSelectedWidgetId(null);
    clearRequestedTab();
    setEditMode(false);
    setSaveError(null);
  }

  async function handleDelete() {
    if (!report || deleting) return;
    setConfirmDelete(false);
    setDeleting(true);
    setSaveError(null);
    try {
      await deleteReport(report.id);
      router.push("/reports");
    } catch (err) {
      const e = err as Error;
      setSaveError(e.message || "Couldn't delete report");
      setDeleting(false);
    }
  }

  // Duplicate the report into a fresh private copy owned by the
  // viewer, then navigate to the copy's editor. Anyone who can view
  // the report (i.e. is on this page) can duplicate it.
  async function handleDuplicate() {
    if (!report || duplicating) return;
    setDuplicating(true);
    setSaveError(null);
    try {
      const copy = await duplicateReport(report.id);
      router.push(`/reports/${copy.id}`);
    } catch (err) {
      const e = err as Error;
      setSaveError(e.message || "Couldn't duplicate report");
      setDuplicating(false);
    }
  }

  // Flip the report between private and org-shared visibility. Gated
  // on edit rights (``canEdit``); the backend enforces this regardless.
  async function handleToggleVisibility() {
    if (!report || togglingVisibility) return;
    const next: ReportSummary["visibility"] =
      report.visibility === "org" ? "private" : "org";
    setTogglingVisibility(true);
    setSaveError(null);
    try {
      const updated = await updateReport(report.id, { visibility: next });
      setReport(updated);
    } catch (err) {
      const e = err as Error;
      setSaveError(e.message || "Couldn't update sharing");
    } finally {
      setTogglingVisibility(false);
    }
  }

  // Restore a chosen version into the live report. Confirmed via the
  // ConfirmModal before this fires. Re-hydrates the canvas from the
  // server's restored snapshot, closes the History panel, and drops
  // back to view mode.
  async function handleRestore() {
    if (!report || !pendingRestore || restoring) return;
    const versionId = pendingRestore.id;
    setRestoring(true);
    setSaveError(null);
    try {
      const restored = await restoreVersion(report.id, versionId);
      hydrateFromReport(restored);
      setEditMode(false);
      setHistoryOpen(false);
      setPendingRestore(null);
    } catch (err) {
      const e = err as Error;
      setSaveError(e.message || "Couldn't restore version");
    } finally {
      setRestoring(false);
    }
  }

  if (authLoading || (!report && !loadError)) {
    return (
      <AppShell>
        <div className="flex h-full items-center justify-center">
          <div
            data-testid="report-editor-loading"
            className="h-6 w-6 animate-spin rounded-full border-2 border-border border-t-accent"
          />
        </div>
      </AppShell>
    );
  }

  if (loadError) {
    return (
      <AppShell>
        <div
          role="alert"
          className="rounded-md border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger"
        >
          {loadError}
        </div>
        <Link
          href="/reports"
          className="mt-3 inline-block text-sm text-accent hover:underline"
        >
          Back to reports
        </Link>
      </AppShell>
    );
  }

  if (!report) return null;

  return (
    <AppShell>
      <div className="flex h-full flex-col" data-testid="report-editor">
      <header className="flex flex-wrap items-center justify-between gap-y-2 border-b border-border bg-surface px-4 py-3">
        <div className="flex items-center gap-3">
          <Link
            href="/reports"
            className="text-sm text-text-muted hover:text-text-primary"
          >
            Reports
          </Link>
          <span className="text-text-muted">/</span>
          {editModeActive ? (
            <input
              type="text"
              value={titleDraft}
              onChange={(e) => setTitleDraft(e.target.value)}
              onBlur={commitTitle}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  // Commit directly (don't rely on blur firing): then drop
                  // focus so the field reads as committed.
                  void commitTitle();
                  e.currentTarget.blur();
                }
              }}
              aria-label="Report title"
              placeholder="Report title"
              data-testid="report-editor-title"
              className="rounded-md border border-transparent bg-transparent px-2 py-1 text-sm font-semibold text-text-primary hover:border-border focus:border-border focus:bg-bg focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
            />
          ) : (
            <span className="text-sm font-semibold text-text-primary">
              {report.name}
            </span>
          )}
          {dirty && (
            <span
              data-testid="report-editor-dirty"
              className="text-xs text-text-muted"
            >
              Unsaved changes
            </span>
          )}
          {!dirty && lastSavedAt && (
            <span className="text-xs text-text-muted">Saved</span>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-1.5 sm:gap-2" data-testid="report-editor-action-group">
          {/* Edit-mode-only: Add widget + Save (+ Cancel when dirty).
              All gated on ``editModeActive`` so small screens (< sm)
              never show edit affordances. */}
          {editModeActive && (
            <button
              type="button"
              onClick={() => setPickerOpen(true)}
              className={btnCanvas}
              data-testid="report-editor-add-widget"
            >
              Add widget
            </button>
          )}
          {editModeActive && (
            <button
              type="button"
              onClick={handleSave}
              disabled={saving || !dirty}
              className="rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-accent-text transition hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-60"
              data-testid="report-editor-save"
            >
              {saving ? "Saving..." : "Save"}
            </button>
          )}
          {editModeActive && dirty && (
            <button
              type="button"
              onClick={handleCancelEdit}
              className={btnCanvas}
              data-testid="report-editor-cancel"
            >
              Cancel
            </button>
          )}

          {/* Both modes: Sharing state, Duplicate, History + Delete. */}
          {/* Visibility: editors get a toggle; non-editors see it read-only. */}
          {canEdit ? (
            <button
              type="button"
              onClick={handleToggleVisibility}
              disabled={togglingVisibility}
              className={btnCanvas}
              data-testid="report-editor-visibility-toggle"
              aria-label="Toggle report sharing"
            >
              <span data-testid="report-editor-visibility">
                {report.visibility === "org" ? "Shared with org" : "Private"}
              </span>
            </button>
          ) : (
            <span
              className="rounded-md border border-border px-3 py-1.5 text-sm text-text-muted"
              data-testid="report-editor-visibility"
            >
              {report.visibility === "org" ? "Shared with org" : "Private"}
            </span>
          )}

          <button
            type="button"
            onClick={handleDuplicate}
            disabled={duplicating}
            className="rounded-md border border-border px-3 py-1.5 text-sm text-text-primary hover:bg-surface-raised disabled:cursor-not-allowed disabled:opacity-60"
            data-testid="report-editor-duplicate"
          >
            {duplicating ? "Duplicating..." : "Duplicate"}
          </button>

          <button
            type="button"
            onClick={() => setHistoryOpen(true)}
            className="rounded-md border border-border px-3 py-1.5 text-sm text-text-primary hover:bg-surface-raised"
            data-testid="report-editor-history"
          >
            History
          </button>
          {canEdit && (
            <button
              type="button"
              onClick={() => setConfirmDelete(true)}
              disabled={deleting}
              className="rounded-md border border-danger px-3 py-1.5 text-sm text-danger hover:bg-danger/10 disabled:cursor-not-allowed disabled:opacity-60"
              data-testid="report-editor-delete"
            >
              {deleting ? "Deleting..." : "Delete"}
            </button>
          )}

          {/* Mode toggle: View mode shows "Edit", edit mode shows "Done".
              Hidden on small screens (editing is desktop-only) and for
              non-owners (editing is owner-only; the backend 403s their
              writes, so an "edit" state would only mislead). */}
          {!isSmallScreen && canEdit && (
            <button
              type="button"
              onClick={() => setEditMode((v) => !v)}
              className={editMode ? btnCanvasActive : btnCanvas}
              data-testid="report-editor-toggle-edit"
            >
              {editMode ? "Done" : "Edit"}
            </button>
          )}
        </div>
      </header>

      {/* Transient success toast. ``role="status"`` (polite live region)
          announces the save to assistive tech; auto-dismisses after ~2s
          via the effect above. Pointer-events-none so it never blocks
          the UI underneath. */}
      {showSavedToast && (
        <div
          role="status"
          data-testid="report-editor-saved-toast"
          className="pointer-events-none fixed bottom-6 left-1/2 z-50 -translate-x-1/2 rounded-md bg-text-primary px-4 py-2 text-sm font-medium text-surface shadow-lg"
        >
          Report saved
        </div>
      )}

      {saveError && (
        <div
          role="alert"
          className="border-b border-danger/30 bg-danger/10 px-4 py-2 text-sm text-danger"
        >
          {saveError}
        </div>
      )}

      {/* Page-scoped notice (TBD-430). `/reports/sources` is ONE constant
          SWR key, so its failure is one fact about the whole canvas and is
          never a per-widget notice. Copy states the REAL blast radius:
          with `sources: []`, `sourceSupportsField` returns `true` by
          design, so some widgets push a filter their source does not
          publish and take a 422 — they render "Couldn't load", not a wrong
          symbol. No acknowledgement, no dismissal, no persistence: it
          disappears when the fetch recovers. */}
      {sourcesError && (
        <div className="border-b border-border px-4 py-2">
          <div
            role="alert"
            data-testid="report-sources-unavailable"
            className="flex items-start gap-2 rounded-md bg-warning-dim px-4 py-3 text-sm text-text-primary"
          >
            <TriangleAlert
              aria-hidden="true"
              className="mt-0.5 h-4 w-4 shrink-0 text-warning"
            />
            <span>
              The data-source catalog is unavailable. Some widgets may be
              missing units or failing to load.
            </span>
          </div>
        </div>
      )}

      <div className="border-b border-border bg-bg px-4 py-3">
        <CanvasFiltersBar value={canvasFilters} onChange={updateCanvasFilters} />
      </div>

      <div className="flex flex-1 overflow-hidden">
        <div
          data-testid="report-canvas-column"
          className="flex-1 overflow-y-auto px-4 py-4"
        >
          {layout.widgets.length === 0 ? (
            <div
              data-testid="report-editor-empty"
              className="rounded-md border border-dashed border-border bg-surface px-6 py-10 text-center"
            >
              <p className="text-sm font-medium text-text-primary">
                This report has no widgets yet
              </p>
              <p className="mx-auto mt-1 max-w-md text-sm text-text-muted">
                Add a widget to see your data. The canvas date applies to
                every widget; accounts and categories are per-widget, so a
                report with no widgets shows nothing.
              </p>
              <div className="mt-4 flex flex-wrap items-center justify-center gap-2">
                {/* "Add widget" is owner-only: a non-owner viewing a shared
                    report can't save, so the picker would only let them
                    build unsavable local changes (backend 403s the PATCH). */}
                {canEdit && (
                  <button
                    type="button"
                    onClick={() => setPickerOpen(true)}
                    className="rounded-md bg-accent px-4 py-2 text-sm font-semibold text-accent-text transition hover:bg-accent-hover"
                    data-testid="report-editor-empty-add-widget"
                  >
                    Add widget
                  </button>
                )}
                <Link
                  href="/reports"
                  className="rounded-md border border-border px-4 py-2 text-sm text-text-primary hover:bg-surface-raised"
                  data-testid="report-editor-empty-templates"
                >
                  Start from a template
                </Link>
              </div>
            </div>
          ) : isSmallScreen ? (
            // Mobile (< sm): read-only single-column stack ordered by
            // grid (y, then x). No grid, no drag/resize, no select/edit
            // chrome — editing is desktop-only.
            <div
              data-testid="reports-canvas-stack"
              className="flex flex-col gap-3"
            >
              {orderWidgetsForStack(layout.widgets).map((w) => {
                const h = mobileStackHeight(w);
                return (
                  <div key={w.id} style={h ? { height: h } : undefined}>
                    {renderReportWidget(w, canvasFilters, false, currency)}
                  </div>
                );
              })}
            </div>
          ) : (
            <Canvas
              layout={layout}
              editMode={editModeActive}
              onLayoutChange={updateLayout}
              renderWidget={(w) => (
                <WidgetShell
                  widgetId={w.id}
                  selected={selectedWidgetId === w.id}
                  editMode={editModeActive}
                  onSelect={() => setSelectedWidgetId(w.id)}
                  onRemove={() => removeWidget(w.id)}
                  widget={w}
                  canvasFilters={canvasFilters}
                  accounts={accounts}
                  categories={categories}
                  // WidgetShell gates chip interactivity on its ``editMode``
                  // prop (``editModeActive`` here): in view mode the chips
                  // render as inert informational spans, so this handler is
                  // only ever invoked in edit mode — no orphan-selection
                  // guard needed.
                  onSelectFilters={() => selectWidgetFilters(w.id)}
                >
                  {renderReportWidget(w, canvasFilters, editModeActive, currency)}
                </WidgetShell>
              )}
            />
          )}
        </div>
      </div>

      {/* Widget editor — an anchored floating popover portaled to the body,
          NOT a flex sibling of the canvas, so selecting a widget never
          reflows / re-clamps the canvas grid. Gated on a resolved
          ``anchorEl`` (set post-commit by ``useWidgetAnchor``). */}
      {editModeActive && selectedWidget && anchorEl && (
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

      <ConfirmModal
        open={confirmDelete}
        title="Delete report"
        message="Delete this report? This can't be undone."
        confirmLabel="Delete"
        variant="danger"
        onConfirm={handleDelete}
        onCancel={() => setConfirmDelete(false)}
      />
      <HistoryPanel
        open={historyOpen}
        reportId={report.id}
        onClose={() => setHistoryOpen(false)}
        onRestore={(v) => setPendingRestore(v)}
      />
      <ConfirmModal
        open={pendingRestore !== null}
        title="Restore version"
        message="Restore this version? Current unsaved changes will be lost."
        confirmLabel="Restore"
        variant="warning"
        onConfirm={handleRestore}
        onCancel={() => setPendingRestore(null)}
      />
    </AppShell>
  );
}
