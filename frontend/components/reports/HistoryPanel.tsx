"use client";

/**
 * Reports v2 — version History panel.
 *
 * Replaces the standalone "Revert to original" button. Lists the
 * report's saved versions newest-first (from ``listVersions``). The
 * original snapshot gets an "Original" badge; every other row shows
 * its ``created_at`` timestamp. Each row offers a Restore action,
 * which the parent confirms before calling ``restoreVersion``.
 */
import { useEffect, useState } from "react";

import { listVersions } from "@/lib/reports/api";
import type { ReportVersionSummary } from "@/lib/reports/types";
import { btnSecondary } from "@/lib/styles";

interface Props {
  open: boolean;
  reportId: number;
  onClose: () => void;
  onRestore: (version: ReportVersionSummary) => void;
}

function formatTimestamp(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function HistoryPanel({
  open,
  reportId,
  onClose,
  onRestore,
}: Props) {
  const [versions, setVersions] = useState<ReportVersionSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setVersions(null);
    setError(null);
    listVersions(reportId)
      .then((vs) => {
        if (!cancelled) setVersions(vs);
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message || "Couldn't load history");
      });
    return () => {
      cancelled = true;
    };
  }, [open, reportId]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-bg/80 p-4"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="history-panel-title"
        data-testid="report-history-panel"
        className="w-full max-w-[min(32rem,calc(100vw-2rem))] max-h-[90vh] overflow-y-auto rounded-lg border border-border bg-surface p-6 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <h3
            id="history-panel-title"
            className="text-lg font-semibold text-text-primary"
          >
            Version history
          </h3>
          <button
            type="button"
            onClick={onClose}
            className="text-sm text-text-muted hover:text-text-primary"
            data-testid="report-history-close"
            aria-label="Close history"
          >
            Close
          </button>
        </div>

        {error && (
          <p
            role="alert"
            className="mt-4 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger"
          >
            {error}
          </p>
        )}

        {!error && versions === null && (
          <p className="mt-4 text-sm text-text-muted">Loading history...</p>
        )}

        {!error && versions !== null && versions.length === 0 && (
          <p className="mt-4 text-sm text-text-muted">No versions yet.</p>
        )}

        {!error && versions !== null && versions.length > 0 && (
          <ul className="mt-4 divide-y divide-border">
            {versions.map((v) => (
              <li
                key={v.id}
                data-testid={`report-history-row-${v.id}`}
                className="flex items-center justify-between gap-3 py-3"
              >
                <div className="flex items-center gap-2">
                  {v.is_original ? (
                    <span
                      data-testid="report-history-original-badge"
                      className="rounded-full bg-accent/15 px-2 py-0.5 text-xs font-medium text-accent"
                    >
                      Original
                    </span>
                  ) : (
                    <span className="text-sm text-text-secondary">
                      {formatTimestamp(v.created_at)}
                    </span>
                  )}
                </div>
                <button
                  type="button"
                  onClick={() => onRestore(v)}
                  className={`${btnSecondary} min-h-[36px]`}
                  data-testid={`report-history-restore-${v.id}`}
                >
                  Restore
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
