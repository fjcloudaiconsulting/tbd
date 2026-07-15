"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import AppShell from "@/components/AppShell";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { hasPlatformPermission } from "@/lib/auth";
import type { AnalyticsResponse, DailyCount } from "@/lib/types";
import {
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
  pageTitle,
} from "@/lib/styles";

const dtFmt = new Intl.DateTimeFormat(undefined, {
  year: "numeric",
  month: "short",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

function sum(series: DailyCount[]): number {
  return series.reduce((acc, row) => acc + (row.count ?? 0), 0);
}

function average(series: DailyCount[]): number {
  if (series.length === 0) return 0;
  return sum(series) / series.length;
}

function formatAvg(value: number): string {
  // The aggregates are integer counts, so an average like 12.4 is honest;
  // anything > 10 rounds to the whole number for legibility.
  if (!Number.isFinite(value)) return "0";
  if (value >= 10) return value.toFixed(0);
  return value.toFixed(1);
}

function formatLastActivity(value: string | null): string {
  if (!value) return "never";
  try {
    return dtFmt.format(new Date(value));
  } catch {
    return value;
  }
}

type MetricRowProps = {
  label: string;
  total: number;
  avg: number;
  series: DailyCount[];
};

function MetricRow({ label, total, avg, series }: MetricRowProps) {
  // Mini sparkline-strip: render a row of fixed-width dots, each cell's
  // height scaled to its proportion of the day-max. No chart library;
  // pure CSS so it stays token-clean.
  const dayMax = series.reduce((acc, row) => Math.max(acc, row.count ?? 0), 0);
  return (
    <div className="border-b border-border-subtle px-6 py-4 last:border-b-0">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <div>
          <div className="text-xs font-medium uppercase tracking-wider text-text-muted">
            {label}
          </div>
          <div className="mt-1 flex items-baseline gap-3">
            <span className="text-2xl font-semibold tabular-nums text-text-primary">
              {total.toLocaleString()}
            </span>
            <span className="text-xs text-text-muted">
              total in window
            </span>
          </div>
        </div>
        <div className="text-right">
          <div className="text-xs uppercase tracking-wider text-text-muted">
            avg/day
          </div>
          <div className="text-lg font-semibold tabular-nums text-text-primary">
            {formatAvg(avg)}
          </div>
        </div>
      </div>
      <div
        className="mt-3 flex items-end gap-[2px] h-8"
        aria-hidden="true"
      >
        {series.map((row) => {
          const ratio = dayMax > 0 ? (row.count ?? 0) / dayMax : 0;
          const heightPct = Math.max(4, ratio * 100);
          return (
            <div
              key={row.date}
              title={`${row.date}: ${row.count}`}
              className="flex-1 min-w-[2px] rounded-sm bg-accent/30"
              style={{ height: `${heightPct}%` }}
            />
          );
        })}
      </div>
    </div>
  );
}

export default function AdminAnalyticsPage() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const [data, setData] = useState<AnalyticsResponse | null>(null);
  const [error, setError] = useState("");
  const [fetching, setFetching] = useState(true);

  useEffect(() => {
    if (loading) return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (!hasPlatformPermission(user, "analytics.view")) {
      router.replace("/dashboard");
    }
  }, [loading, user, router]);

  useEffect(() => {
    if (loading || !user || !hasPlatformPermission(user, "analytics.view")) {
      return;
    }
    // eslint-disable-next-line react-hooks/set-state-in-effect -- loading flag set before an in-effect fetch; proper fix arrives with the SWR data-hook migration
    setFetching(true);
    apiFetch<AnalyticsResponse>("/api/v1/admin/analytics?days=30")
      .then((d) => setData(d))
      .catch((err) => setError(extractErrorMessage(err, "Failed to load")))
      .finally(() => setFetching(false));
  }, [loading, user]);

  if (loading || !user || !hasPlatformPermission(user, "analytics.view")) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner />
      </div>
    );
  }

  return (
    <AppShell>
      <h1 className={pageTitle}>System usage analytics</h1>

      {error && (
        <div className={`${errorCls} mb-4`} role="alert">
          {error}
        </div>
      )}

      <div className="mb-2 text-xs text-text-muted">
        {data
          ? `Last ${data.window_days} days, as of ${formatLastActivity(data.generated_at)}`
          : fetching
            ? "Loading…"
            : ""}
      </div>

      <div className={`${card} mb-6`}>
        <div className={cardHeader}>
          <h2 className={cardTitle}>Activity (last 30 days)</h2>
        </div>
        {fetching && !data && (
          <div className="px-6 py-6 text-center text-text-muted">
            Loading…
          </div>
        )}
        {data && (
          <>
            <MetricRow
              label="Successful logins"
              total={sum(data.logins_by_day)}
              avg={average(data.logins_by_day)}
              series={data.logins_by_day}
            />
            <MetricRow
              label="Transactions created"
              total={sum(data.tx_writes_by_day)}
              avg={average(data.tx_writes_by_day)}
              series={data.tx_writes_by_day}
            />
            <MetricRow
              label="Rows imported"
              total={sum(data.imports_by_day)}
              avg={average(data.imports_by_day)}
              series={data.imports_by_day}
            />
          </>
        )}
      </div>

      <div className={`${card} mb-6`}>
        <div className={cardHeader}>
          <h2 className={cardTitle}>Top organizations</h2>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-y border-border text-left text-xs uppercase tracking-wider text-text-muted">
                <th className="px-6 py-3 w-16">Rank</th>
                <th className="px-6 py-3">Organization</th>
                <th className="px-6 py-3 text-right">Transactions</th>
              </tr>
            </thead>
            <tbody>
              {fetching && !data && (
                <tr>
                  <td
                    colSpan={3}
                    className="px-6 py-6 text-center text-text-muted"
                  >
                    Loading…
                  </td>
                </tr>
              )}
              {data && data.top_orgs_by_tx_volume.length === 0 && (
                <tr>
                  <td
                    colSpan={3}
                    className="px-6 py-6 text-center text-text-muted"
                  >
                    No transaction activity in the window.
                  </td>
                </tr>
              )}
              {data?.top_orgs_by_tx_volume.map((row) => (
                <tr
                  key={row.org_id}
                  className="border-b border-border-subtle last:border-b-0"
                >
                  <td className="px-6 py-3 font-mono text-text-muted tabular-nums">
                    {row.rank}
                  </td>
                  <td className="px-6 py-3 text-text-primary">
                    {row.org_name}
                    <span className="ml-1 text-text-muted">
                      (#{row.org_id})
                    </span>
                  </td>
                  <td className="px-6 py-3 text-right font-mono text-text-secondary tabular-nums">
                    {row.tx_count.toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className={`${card} mb-6`}>
        <div className={cardHeader}>
          <h2 className={cardTitle}>Dormant organizations</h2>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-y border-border text-left text-xs uppercase tracking-wider text-text-muted">
                <th className="px-6 py-3">Organization</th>
                <th className="px-6 py-3">Last activity</th>
                <th className="px-6 py-3 text-right">Days since</th>
              </tr>
            </thead>
            <tbody>
              {fetching && !data && (
                <tr>
                  <td
                    colSpan={3}
                    className="px-6 py-6 text-center text-text-muted"
                  >
                    Loading…
                  </td>
                </tr>
              )}
              {data && data.dormant_orgs.length === 0 && (
                <tr>
                  <td
                    colSpan={3}
                    className="px-6 py-6 text-center text-text-muted"
                  >
                    All organizations are active.
                  </td>
                </tr>
              )}
              {data?.dormant_orgs.map((row) => (
                <tr
                  key={row.org_id}
                  className="border-b border-border-subtle last:border-b-0"
                >
                  <td className="px-6 py-3 text-text-primary">
                    {row.org_name}
                    <span className="ml-1 text-text-muted">
                      (#{row.org_id})
                    </span>
                  </td>
                  <td className="px-6 py-3 text-text-secondary tabular-nums">
                    {formatLastActivity(row.last_tx_at)}
                  </td>
                  <td className="px-6 py-3 text-right font-mono text-text-secondary tabular-nums">
                    {row.days_since_last_activity ?? (
                      <span className="text-text-muted">never</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </AppShell>
  );
}
