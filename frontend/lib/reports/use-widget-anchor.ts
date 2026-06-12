import { useEffect, useState } from "react";

/**
 * Resolve the selected widget's shell DOM node into an ``anchorEl`` so the
 * widget-editor popover can float anchored to it (rather than consuming
 * flex width and reflowing the canvas). Shared by the saved-report editor
 * (``/reports/[id]``) and the draft creator (``/reports/new``).
 *
 * The effect runs post-commit, so the anchor resolves on the render AFTER
 * selection — page-level tests must ``waitFor`` the popover.
 *
 * @param selectedWidgetId the currently selected widget id, or ``null``.
 * @param enabled gate that mirrors each page's edit-mode contract. The
 *   saved editor passes ``editModeActive`` (desktop + owner + edit toggle);
 *   the draft creator is ALWAYS in edit mode, so it passes ``true``.
 * @param reResolveTrigger the widgets list (``layout.widgets`` /
 *   ``layout?.widgets``). A canvas re-render re-creates the shell node, so
 *   re-resolving when the list identity changes keeps the anchor fresh.
 */
export function useWidgetAnchor(
  selectedWidgetId: string | null,
  enabled: boolean,
  reResolveTrigger: unknown,
): HTMLElement | null {
  const [anchorEl, setAnchorEl] = useState<HTMLElement | null>(null);

  useEffect(() => {
    if (!enabled || !selectedWidgetId) {
      setAnchorEl(null);
      return;
    }
    setAnchorEl(
      document.querySelector(
        `[data-widget-shell="${selectedWidgetId}"]`,
      ) as HTMLElement | null,
    );
    // ``reResolveTrigger`` is the re-resolve dependency (the widgets list).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, selectedWidgetId, reResolveTrigger]);

  return anchorEl;
}
