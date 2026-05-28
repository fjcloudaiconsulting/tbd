"use client";

/**
 * Reports v2 — list page.
 *
 * Shows reports visible to the caller (own + org-shared) and a
 * "New report" button that creates an empty private report and
 * navigates to the editor. Templates tab + visibility toggle are
 * PR4 work; this page keeps the substrate ready for them.
 *
 * The page guards the ``feature_reports_v2`` signal client-side; if
 * the operator hasn't flipped the flag, the user shouldn't be here.
 * (The backend hard-404s its routes too, so even direct nav fails.)
 */
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";

import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";
import { createReport, listReports } from "@/lib/reports/api";
import type { ReportSummary } from "@/lib/reports/types";

export default function ReportsListPage() {
  const router = useRouter();
  const { user, loading: authLoading, featureReportsV2 } = useAuth();
  const [reports, setReports] = useState<ReportSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

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
    let cancelled = false;
    listReports()
      .then((data) => {
        if (!cancelled) setReports(data);
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message || "Couldn't load reports");
      });
    return () => {
      cancelled = true;
    };
    // ``router`` is intentionally omitted — useRouter() identity is
    // not guaranteed stable across renders, and refiring the list
    // effect on every render would churn the list.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user, authLoading, featureReportsV2]);

  async function handleNewReport() {
    if (creating) return;
    setCreating(true);
    try {
      const created = await createReport({
        name: "Untitled report",
        visibility: "private",
        layout_json: { version: 1, widgets: [] },
        canvas_filters_json: {},
      });
      router.push(`/reports/${created.id}`);
    } catch (err) {
      const e = err as Error;
      setError(e.message || "Couldn't create report");
      setCreating(false);
    }
  }

  if (authLoading) {
    return (
      <AppShell>
        <div className="flex h-full items-center justify-center">
          <div className="h-6 w-6 animate-spin rounded-full border-2 border-border border-t-accent" />
        </div>
      </AppShell>
    );
  }

  return (
    <AppShell>
      <header className="mb-6 flex items-center justify-between">
        <div data-tour-id="reports.title">
          <h1 className="text-2xl font-semibold text-text-primary">Reports</h1>
          <p className="text-sm text-text-muted">
            Build a layout of KPIs and charts over your transactions.
          </p>
        </div>
        <button
          type="button"
          onClick={handleNewReport}
          disabled={creating}
          className="inline-flex items-center gap-2 rounded-md bg-accent px-4 py-2 text-sm font-semibold text-accent-foreground transition hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-60"
        >
          {creating ? "Creating..." : "New report"}
        </button>
      </header>

      {error && (
        <div
          role="alert"
          className="mb-4 rounded-md border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger"
        >
          {error}
        </div>
      )}

      {reports === null && !error ? (
        <div className="rounded-md border border-border bg-surface px-4 py-6 text-sm text-text-muted">
          Loading...
        </div>
      ) : reports && reports.length === 0 ? (
        <div
          data-testid="reports-empty-state"
          className="rounded-md border border-dashed border-border bg-surface px-6 py-10 text-center"
        >
          <p className="text-sm font-medium text-text-primary">
            No reports yet
          </p>
          <p className="mt-1 text-sm text-text-muted">
            Start a blank canvas to build your first one.
          </p>
        </div>
      ) : (
        <ul className="divide-y divide-border rounded-md border border-border bg-surface">
          {(reports ?? []).map((r) => (
            <li key={r.id}>
              <Link
                href={`/reports/${r.id}`}
                className="block px-4 py-3 hover:bg-bg-elevated"
                data-testid={`report-row-${r.id}`}
              >
                <div className="flex items-center justify-between">
                  <div>
                    <div className="font-medium text-text-primary">
                      {r.name}
                    </div>
                    {r.description && (
                      <div className="text-sm text-text-muted">
                        {r.description}
                      </div>
                    )}
                  </div>
                  <span className="text-[11px] font-medium uppercase tracking-wider text-text-muted">
                    {r.visibility}
                  </span>
                </div>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </AppShell>
  );
}
