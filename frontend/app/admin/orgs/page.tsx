"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import AppShell from "@/components/AppShell";
import ConfirmModal from "@/components/ui/ConfirmModal";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { isSuperadmin } from "@/lib/auth";
import {
  btnSecondary,
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
  input,
  pageTitle,
  success as successCls,
} from "@/lib/styles";

type OrgRow = {
  id: number;
  name: string;
  plan_slug: string | null;
  subscription_status: string | null;
  trial_end: string | null;
  user_count: number;
  active_user_count: number;
  created_at: string | null;
  last_user_created_at: string | null;
};

type OrgListResponse = {
  items: OrgRow[];
  total: number;
  limit: number;
  offset: number;
};

const PAGE_SIZE = 50;

export default function AdminOrgsPage() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const [data, setData] = useState<OrgListResponse | null>(null);
  const [error, setError] = useState("");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const [fetching, setFetching] = useState(true);
  const [sweepConfirmOpen, setSweepConfirmOpen] = useState(false);
  const [sweepBusy, setSweepBusy] = useState(false);
  const [sweepNotice, setSweepNotice] = useState("");

  async function runSweep() {
    setSweepConfirmOpen(false);
    setSweepBusy(true);
    setSweepNotice("");
    setError("");
    try {
      const res = await apiFetch<{ deleted_count: number }>(
        "/api/v1/admin/orgs/feature-overrides/sweep-expired",
        { method: "POST" },
      );
      setSweepNotice(`Removed ${res.deleted_count} expired overrides.`);
    } catch (err) {
      setError(extractErrorMessage(err, "Sweep failed"));
    } finally {
      setSweepBusy(false);
    }
  }

  useEffect(() => {
    if (loading) return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (!isSuperadmin(user)) {
      router.replace("/dashboard");
    }
  }, [loading, user, router]);

  useEffect(() => {
    if (loading || !user || !isSuperadmin(user)) return;
    setFetching(true);
    const params = new URLSearchParams({
      limit: String(PAGE_SIZE),
      offset: String(offset),
    });
    if (q.trim()) params.set("q", q.trim());
    apiFetch<OrgListResponse>(`/api/v1/admin/orgs?${params.toString()}`)
      .then((d) => setData(d))
      .catch((err) => setError(extractErrorMessage(err, "Failed to load")))
      .finally(() => setFetching(false));
  }, [loading, user, q, offset]);

  if (loading || !user || !isSuperadmin(user)) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner />
      </div>
    );
  }

  return (
    <AppShell>
      <div className="mb-8 flex items-center justify-between gap-4">
        <h1 className={`${pageTitle} mb-0`}>Organizations</h1>
        <button
          type="button"
          onClick={() => setSweepConfirmOpen(true)}
          disabled={sweepBusy}
          className={btnSecondary}
        >
          {sweepBusy ? "Sweeping…" : "Sweep expired overrides"}
        </button>
      </div>

      {error && (
        <div className={`${errorCls} mb-4`} role="alert">
          {error}
        </div>
      )}

      {sweepNotice && (
        <div className={`${successCls} mb-4`} role="status">
          {sweepNotice}
        </div>
      )}

      <ConfirmModal
        open={sweepConfirmOpen}
        title="Sweep expired overrides"
        message="Permanently delete every feature override row whose expires_at is in the past. This cannot be undone."
        confirmLabel="Sweep"
        cancelLabel="Cancel"
        variant="warning"
        onConfirm={runSweep}
        onCancel={() => setSweepConfirmOpen(false)}
      />

      <div className={`${card} mb-6`}>
        <div className={cardHeader}>
          <h2 className={cardTitle}>All organizations</h2>
        </div>
        <div className="px-6 py-4">
          <input
            type="search"
            value={q}
            onChange={(e) => {
              setOffset(0);
              setQ(e.target.value);
            }}
            placeholder="Search by name…"
            className={`${input} w-full max-w-sm`}
            aria-label="Search organizations"
          />
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-y border-border text-left text-xs uppercase tracking-wider text-text-muted">
                <th className="px-6 py-3">Name</th>
                <th className="px-6 py-3">Plan</th>
                <th className="px-6 py-3">Status</th>
                <th className="px-6 py-3">Users</th>
                <th className="px-6 py-3">Newest member</th>
                <th className="px-6 py-3">Created</th>
              </tr>
            </thead>
            <tbody>
              {fetching && (
                <tr>
                  <td colSpan={6} className="px-6 py-6 text-center text-text-muted">
                    Loading…
                  </td>
                </tr>
              )}
              {!fetching && data?.items.length === 0 && (
                <tr>
                  <td colSpan={6} className="px-6 py-6 text-center text-text-muted">
                    No organizations match.
                  </td>
                </tr>
              )}
              {!fetching &&
                data?.items.map((row) => (
                  <tr key={row.id} className="border-b border-border-subtle">
                    <td className="px-6 py-3">
                      <Link
                        href={`/admin/orgs/${row.id}`}
                        className="text-accent hover:text-accent-hover"
                      >
                        {row.name}
                      </Link>
                    </td>
                    <td className="px-6 py-3 text-text-secondary">
                      {row.plan_slug ?? "—"}
                    </td>
                    <td className="px-6 py-3 text-text-secondary">
                      {row.subscription_status ?? "—"}
                    </td>
                    <td className="px-6 py-3 text-text-secondary tabular-nums">
                      {row.active_user_count} / {row.user_count}
                    </td>
                    <td className="px-6 py-3 text-text-secondary tabular-nums">
                      {row.last_user_created_at?.slice(0, 10) ?? "—"}
                    </td>
                    <td className="px-6 py-3 text-text-secondary tabular-nums">
                      {row.created_at?.slice(0, 10) ?? "—"}
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>

        {data && data.total > PAGE_SIZE && (
          <div className="flex items-center justify-between px-6 py-3 text-xs text-text-muted">
            <span>
              {offset + 1}–{Math.min(offset + PAGE_SIZE, data.total)} of{" "}
              {data.total}
            </span>
            <div className="flex gap-2">
              <button
                type="button"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                className="rounded-md border border-border px-3 py-1 disabled:opacity-50"
              >
                Prev
              </button>
              <button
                type="button"
                disabled={offset + PAGE_SIZE >= data.total}
                onClick={() => setOffset(offset + PAGE_SIZE)}
                className="rounded-md border border-border px-3 py-1 disabled:opacity-50"
              >
                Next
              </button>
            </div>
          </div>
        )}
      </div>
    </AppShell>
  );
}
