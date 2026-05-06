"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import AppShell from "@/components/AppShell";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { isSuperadmin } from "@/lib/auth";
import type { AuditEvent, AuditEventListResponse } from "@/lib/types";
import {
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
  input,
  pageTitle,
} from "@/lib/styles";

const PAGE_SIZE = 50;

const dtFmt = new Intl.DateTimeFormat(undefined, {
  year: "numeric",
  month: "short",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

function shortRequestId(value: string | null): string {
  if (!value) return "";
  return value.length > 12 ? value.slice(0, 12) : value;
}

export default function AdminAuditPage() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const [data, setData] = useState<AuditEventListResponse | null>(null);
  const [error, setError] = useState("");
  const [eventTypeInput, setEventTypeInput] = useState("");
  const [outcomeInput, setOutcomeInput] = useState("");
  const [targetOrgInput, setTargetOrgInput] = useState("");
  const [offset, setOffset] = useState(0);
  const [fetching, setFetching] = useState(true);

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
    if (eventTypeInput.trim()) params.set("event_type", eventTypeInput.trim());
    if (outcomeInput) params.set("outcome", outcomeInput);
    const targetOrg = targetOrgInput.trim();
    if (targetOrg && /^[1-9][0-9]*$/.test(targetOrg)) {
      params.set("target_org_id", targetOrg);
    }
    apiFetch<AuditEventListResponse>(
      `/api/v1/admin/audit?${params.toString()}`,
    )
      .then((d) => setData(d))
      .catch((err) => setError(extractErrorMessage(err, "Failed to load")))
      .finally(() => setFetching(false));
  }, [loading, user, eventTypeInput, outcomeInput, targetOrgInput, offset]);

  if (loading || !user || !isSuperadmin(user)) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner />
      </div>
    );
  }

  return (
    <AppShell>
      <h1 className={pageTitle}>Audit log</h1>

      {error && (
        <div className={`${errorCls} mb-4`} role="alert">
          {error}
        </div>
      )}

      <div className={`${card} mb-6`}>
        <div className={cardHeader}>
          <h2 className={cardTitle}>Recent events</h2>
        </div>
        <div className="grid grid-cols-1 gap-3 px-6 py-4 sm:grid-cols-3">
          <input
            type="search"
            value={eventTypeInput}
            onChange={(e) => {
              setOffset(0);
              setEventTypeInput(e.target.value);
            }}
            placeholder="Event type (exact)"
            className={input}
            aria-label="Filter by event type"
          />
          <select
            value={outcomeInput}
            onChange={(e) => {
              setOffset(0);
              setOutcomeInput(e.target.value);
            }}
            className={input}
            aria-label="Filter by outcome"
          >
            <option value="">All outcomes</option>
            <option value="success">Success</option>
            <option value="failure">Failure</option>
          </select>
          <input
            type="text"
            inputMode="numeric"
            value={targetOrgInput}
            onChange={(e) => {
              setOffset(0);
              setTargetOrgInput(e.target.value);
            }}
            placeholder="Target org ID"
            className={input}
            aria-label="Filter by target org id"
          />
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-y border-border text-left text-xs uppercase tracking-wider text-text-muted">
                <th className="px-6 py-3">When</th>
                <th className="px-6 py-3">Event type</th>
                <th className="px-6 py-3">Actor email</th>
                <th className="px-6 py-3">Target org</th>
                <th className="px-6 py-3">Outcome</th>
                <th className="px-6 py-3">Request ID</th>
                <th className="px-6 py-3">IP</th>
              </tr>
            </thead>
            <tbody>
              {fetching && (
                <tr>
                  <td
                    colSpan={7}
                    className="px-6 py-6 text-center text-text-muted"
                  >
                    Loading…
                  </td>
                </tr>
              )}
              {!fetching && data?.items.length === 0 && (
                <tr>
                  <td
                    colSpan={7}
                    className="px-6 py-6 text-center text-text-muted"
                  >
                    No audit events match.
                  </td>
                </tr>
              )}
              {!fetching &&
                data?.items.map((row: AuditEvent) => (
                  <tr key={row.id} className="border-b border-border-subtle">
                    <td className="px-6 py-3 text-text-secondary tabular-nums">
                      {dtFmt.format(new Date(row.created_at))}
                    </td>
                    <td className="px-6 py-3 text-text-primary">
                      {row.event_type}
                    </td>
                    <td className="px-6 py-3 text-text-secondary">
                      {row.actor_email}
                    </td>
                    <td className="px-6 py-3 text-text-secondary">
                      {row.target_org_name ?? "-"}
                      {row.target_org_id != null && (
                        <span className="ml-1 text-text-muted">
                          (#{row.target_org_id})
                        </span>
                      )}
                    </td>
                    <td className="px-6 py-3">
                      <span
                        className={
                          row.outcome === "success"
                            ? "rounded-full bg-success/10 px-2 py-0.5 text-xs font-semibold uppercase tracking-wider text-success"
                            : "rounded-full bg-danger/10 px-2 py-0.5 text-xs font-semibold uppercase tracking-wider text-danger"
                        }
                      >
                        {row.outcome}
                      </span>
                    </td>
                    <td
                      className="px-6 py-3 font-mono text-xs text-text-muted"
                      title={row.request_id ?? ""}
                    >
                      {shortRequestId(row.request_id)}
                    </td>
                    <td className="px-6 py-3 font-mono text-xs text-text-muted">
                      {row.ip_address ?? "-"}
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
