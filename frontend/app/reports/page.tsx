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
import {
  createFromTemplate,
  createReport,
  listReports,
  listTemplates,
} from "@/lib/reports/api";
import type { ReportSummary, ReportTemplate } from "@/lib/reports/types";

export default function ReportsListPage() {
  const router = useRouter();
  const { user, loading: authLoading, featureReportsV2 } = useAuth();
  const [reports, setReports] = useState<ReportSummary[] | null>(null);
  const [templates, setTemplates] = useState<ReportTemplate[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [usingTemplate, setUsingTemplate] = useState<string | null>(null);

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
    // Templates load independently — a failure here must not block the
    // reports list, so it swallows its own error (falls back to []).
    listTemplates()
      .then((data) => {
        if (!cancelled) setTemplates(data);
      })
      .catch(() => {
        if (!cancelled) setTemplates([]);
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

  async function handleUseTemplate(t: ReportTemplate) {
    if (usingTemplate) return;
    setUsingTemplate(t.key);
    try {
      const created = await createFromTemplate(t);
      router.push(`/reports/${created.id}`);
    } catch (err) {
      const e = err as Error;
      setError(e.message || "Couldn't create report from template");
      setUsingTemplate(null);
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

      {templates && templates.length > 0 && (
        <section className="mb-8" data-testid="reports-templates">
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-text-muted">
            Start from a template
          </h2>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {templates.map((t) => (
              <div
                key={t.key}
                data-testid={`report-template-${t.key}`}
                className="flex flex-col rounded-md border border-border bg-surface p-4"
              >
                <div className="font-medium text-text-primary">{t.name}</div>
                <p className="mt-1 flex-1 text-sm text-text-muted">
                  {t.description}
                </p>
                <button
                  type="button"
                  onClick={() => handleUseTemplate(t)}
                  disabled={usingTemplate !== null}
                  className="mt-3 inline-flex items-center justify-center gap-2 self-start rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-accent-foreground transition hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {usingTemplate === t.key ? "Creating..." : "Use template"}
                </button>
              </div>
            ))}
          </div>
        </section>
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
            Start from a template above, or create a blank report.
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
