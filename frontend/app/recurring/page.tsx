"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import AppShell from "@/components/AppShell";
import HelpAnchor from "@/components/HelpAnchor";
import Spinner from "@/components/ui/Spinner";
import ConfirmModal from "@/components/ui/ConfirmModal";
import Pagination from "@/components/ui/Pagination";
import SortableHeader from "@/components/ui/SortableHeader";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { formatAmount } from "@/lib/format";
import {
  useTableState,
  paginate,
  pageCount,
  type SortDir,
} from "@/lib/hooks/use-table-state";
import { SORT_KEY_RECURRING } from "@/lib/hooks/persisted-keys";
import { btnSecondary, card, cardHeader, cardTitle, error as errorCls, success as successCls, pageTitle } from "@/lib/styles";
import type { RecurringTransaction } from "@/lib/types";

const FREQ_LABELS: Record<string, string> = {
  weekly: "Weekly",
  biweekly: "Every 2 weeks",
  monthly: "Monthly",
  quarterly: "Quarterly",
  yearly: "Yearly",
};

// Sort field identifiers for the recurring tables.
type SortField =
  | "description"
  | "account"
  | "category"
  | "frequency"
  | "next_due_date"
  | "amount";

const ALLOWED_SORT_FIELDS: readonly SortField[] = [
  "description",
  "account",
  "category",
  "frequency",
  "next_due_date",
  "amount",
];

// Comparator helpers. Nulls/empties always sort last regardless of direction.
function cmpString(a: string | null | undefined, b: string | null | undefined): number {
  const av = a ?? "";
  const bv = b ?? "";
  if (!av && !bv) return 0;
  if (!av) return 1; // null/empty last
  if (!bv) return -1;
  return av.localeCompare(bv, undefined, { sensitivity: "base" });
}

function cmpNumber(a: number, b: number): number {
  return a - b;
}

function sortRecurring(
  rows: RecurringTransaction[],
  field: SortField,
  dir: SortDir,
): RecurringTransaction[] {
  const factor = dir === "asc" ? 1 : -1;
  const sorted = [...rows].sort((a, b) => {
    switch (field) {
      case "description":
        return factor * cmpString(a.description, b.description);
      case "account":
        return factor * cmpString(a.account_name, b.account_name);
      case "category": {
        // Nulls last in BOTH directions: compare null-ness outside the factor.
        const an = a.category_name ?? "";
        const bn = b.category_name ?? "";
        if (!an && !bn) return 0;
        if (!an) return 1;
        if (!bn) return -1;
        return factor * cmpString(an, bn);
      }
      case "frequency":
        return factor * cmpString(
          FREQ_LABELS[a.frequency] ?? a.frequency,
          FREQ_LABELS[b.frequency] ?? b.frequency,
        );
      case "next_due_date":
        // ISO date strings (YYYY-MM-DD) sort chronologically as strings.
        return factor * cmpString(a.next_due_date, b.next_due_date);
      case "amount":
        return factor * cmpNumber(a.amount, b.amount);
      default:
        return 0;
    }
  });
  return sorted;
}

interface RecurringTableProps {
  title: string;
  storageKey: string;
  items: RecurringTransaction[];
  paused?: boolean;
  emptyLabel: string;
  onStop?: (item: RecurringTransaction) => void;
  onResume?: (item: RecurringTransaction) => void;
  onDelete: (id: number) => void;
  testId: string;
}

function RecurringTable({
  title,
  storageKey,
  items,
  paused = false,
  emptyLabel,
  onStop,
  onResume,
  onDelete,
  testId,
}: RecurringTableProps) {
  const { sortField, sortDir, setSort, page, setPage, pageSize, setPageSize } =
    useTableState<SortField>({
      key: storageKey,
      defaultSortField: "next_due_date",
      defaultSortDir: "asc",
      allowedSortFields: ALLOWED_SORT_FIELDS,
    });

  const sorted = useMemo(
    () => sortRecurring(items, sortField, sortDir),
    [items, sortField, sortDir],
  );
  const pageRows = useMemo(
    () => paginate(sorted, page, pageSize),
    [sorted, page, pageSize],
  );
  const showPagination = pageCount(items.length, pageSize) > 1;

  // Click a header: toggle direction if already the active column, else
  // switch to that column starting ascending.
  const handleSort = useCallback(
    (field: string) => {
      const f = field as SortField;
      if (f === sortField) {
        setSort(f, sortDir === "asc" ? "desc" : "asc");
      } else {
        setSort(f, "asc");
      }
    },
    [sortField, sortDir, setSort],
  );

  return (
    <div className={`${card} overflow-x-auto`} data-testid={testId}>
      <div className={cardHeader}>
        <h2 className={cardTitle}>
          {title} ({items.length})
        </h2>
      </div>

      {/* Desktop/tablet table (md+) */}
      <div className="hidden md:block">
        <table className="w-full">
          <thead>
            <tr className="border-b border-border-subtle">
              <SortableHeader
                label="Name"
                field="description"
                activeField={sortField}
                dir={sortDir}
                onSort={handleSort}
              />
              <SortableHeader
                label="Account"
                field="account"
                activeField={sortField}
                dir={sortDir}
                onSort={handleSort}
              />
              <SortableHeader
                label="Category"
                field="category"
                activeField={sortField}
                dir={sortDir}
                onSort={handleSort}
              />
              <SortableHeader
                label="Frequency"
                field="frequency"
                activeField={sortField}
                dir={sortDir}
                onSort={handleSort}
              />
              <SortableHeader
                label="Next due"
                field="next_due_date"
                activeField={sortField}
                dir={sortDir}
                onSort={handleSort}
              />
              <SortableHeader
                label="Amount"
                field="amount"
                activeField={sortField}
                dir={sortDir}
                onSort={handleSort}
                align="right"
              />
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody className="divide-y divide-border-subtle">
            {pageRows.map((r) => (
              <tr
                key={r.id}
                data-testid="recurring-row"
                data-description={r.description}
                className={`transition-colors hover:bg-surface-raised ${paused ? "opacity-50" : ""}`}
              >
                <td className="px-3 py-3 text-sm text-text-primary">
                  {r.description}
                  {!paused && r.auto_settle && (
                    <span className="ml-1.5 rounded bg-success-dim px-1.5 py-0.5 text-[10px] font-medium text-success">
                      auto
                    </span>
                  )}
                </td>
                <td className="px-3 py-3 text-sm text-text-secondary">{r.account_name}</td>
                <td className="px-3 py-3 text-sm text-text-secondary">{r.category_name}</td>
                <td className="px-3 py-3 text-xs text-text-muted">
                  {FREQ_LABELS[r.frequency] ?? r.frequency}
                </td>
                <td className="px-3 py-3 text-sm tabular-nums text-text-secondary">
                  {r.next_due_date}
                </td>
                <td
                  className={`px-3 py-3 text-right text-sm font-medium tabular-nums ${r.type === "income" ? "text-success" : "text-danger"}`}
                >
                  {r.type === "income" ? "+" : "-"}
                  {formatAmount(r.amount)}
                </td>
                <td className="px-3 py-3">
                  <span className="flex justify-end gap-2">
                    {paused ? (
                      <button
                        onClick={() => onResume?.(r)}
                        className="min-h-[44px] text-xs text-text-muted hover:text-accent"
                      >
                        Resume
                      </button>
                    ) : (
                      <button
                        onClick={() => onStop?.(r)}
                        className="min-h-[44px] text-xs text-text-muted hover:text-accent"
                      >
                        Stop
                      </button>
                    )}
                    <button
                      onClick={() => onDelete(r.id)}
                      className="min-h-[44px] text-xs text-text-muted hover:text-danger"
                    >
                      Delete
                    </button>
                  </span>
                </td>
              </tr>
            ))}
            {items.length === 0 && (
              <tr>
                <td colSpan={7} className="px-6 py-8 text-center text-sm text-text-muted">
                  {emptyLabel}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {/* Mobile card layout (below md) */}
      <div className="md:hidden flex flex-col gap-3 p-3">
        {pageRows.map((r) => (
          <article
            key={r.id}
            className={`flex flex-col gap-2 rounded-lg border border-border bg-surface p-4 ${paused ? "opacity-60" : ""}`}
          >
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-medium text-text-primary">
                  {r.description}
                  {!paused && r.auto_settle && (
                    <span className="ml-1.5 rounded bg-success-dim px-1.5 py-0.5 text-[10px] font-medium text-success">
                      auto
                    </span>
                  )}
                </div>
                <div className="mt-0.5 text-xs text-text-muted tabular-nums">
                  Next: {r.next_due_date} &middot; {r.account_name}
                </div>
              </div>
              <div
                className={`shrink-0 text-right text-sm font-semibold tabular-nums ${r.type === "income" ? "text-success" : "text-danger"}`}
              >
                {r.type === "income" ? "+" : "-"}
                {formatAmount(r.amount)}
              </div>
            </div>
            {r.category_name && (
              <div className="text-xs text-text-secondary truncate">{r.category_name}</div>
            )}
            <div className="text-xs text-text-muted">{FREQ_LABELS[r.frequency] ?? r.frequency}</div>
            <div className="flex flex-wrap gap-2 pt-2 border-t border-border-subtle">
              {paused ? (
                <button
                  onClick={() => onResume?.(r)}
                  aria-label={`Resume: ${r.description}`}
                  className="min-h-[44px] px-3 rounded-md border border-border text-sm text-text-secondary"
                >
                  Resume
                </button>
              ) : (
                <button
                  onClick={() => onStop?.(r)}
                  aria-label={`Stop: ${r.description}`}
                  className="min-h-[44px] px-3 rounded-md border border-border text-sm text-text-secondary"
                >
                  Stop
                </button>
              )}
              <button
                onClick={() => onDelete(r.id)}
                aria-label={`Delete: ${r.description}`}
                className="min-h-[44px] px-3 rounded-md border border-border text-sm text-danger"
              >
                Delete
              </button>
            </div>
          </article>
        ))}
        {items.length === 0 && (
          <div className="px-4 py-8 text-center text-sm text-text-muted">{emptyLabel}</div>
        )}
      </div>

      {showPagination && (
        <div className="border-t border-border-subtle px-3">
          <Pagination
            page={page}
            pageSize={pageSize}
            total={items.length}
            onPageChange={setPage}
            onPageSizeChange={setPageSize}
          />
        </div>
      )}
    </div>
  );
}

export default function RecurringPage() {
  const { user, loading } = useAuth();
  const [items, setItems] = useState<RecurringTransaction[]>([]);
  const [fetching, setFetching] = useState(true);
  const [error, setError] = useState("");
  const [successMsg, setSuccessMsg] = useState("");
  const [confirmStop, setConfirmStop] = useState<{ id: number; description: string } | null>(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState<number | null>(null);

  const reload = useCallback(async () => {
    const data = await apiFetch<RecurringTransaction[]>("/api/v1/recurring");
    setItems(data ?? []);
    setFetching(false);
  }, []);

  useEffect(() => {
    if (!loading && user) reload().catch(() => setFetching(false));
  }, [loading, user, reload]);

  async function handleStop(item: RecurringTransaction) {
    setConfirmStop({ id: item.id, description: item.description });
  }

  async function doStop(id: number, description: string) {
    setError(""); setSuccessMsg("");
    try {
      const res = await apiFetch<{ pending_removed: number }>(`/api/v1/recurring/${id}/stop`, { method: "POST" });
      setSuccessMsg(`Stopped "${description}". ${res?.pending_removed ?? 0} pending transaction(s) removed.`);
      await reload();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  async function handleResume(item: RecurringTransaction) {
    try {
      await apiFetch(`/api/v1/recurring/${item.id}`, {
        method: "PUT",
        body: JSON.stringify({ is_active: true }),
      });
      await reload();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  async function handleDelete(id: number) {
    setConfirmDeleteId(id);
  }

  async function doDelete(id: number) {
    setError(""); setSuccessMsg("");
    try {
      const res = await apiFetch<{ pending_removed: number }>(`/api/v1/recurring/${id}`, { method: "DELETE" });
      setSuccessMsg(`Deleted. ${res?.pending_removed ?? 0} pending transaction(s) removed.`);
      await reload();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  async function handleGenerate() {
    setError(""); setSuccessMsg("");
    try {
      const res = await apiFetch<{
        generated: number; settled: number; pending: number; period_end: string;
      }>("/api/v1/recurring/generate", { method: "POST" });
      const through = res?.period_end
        ? new Date(`${res.period_end}T00:00:00`).toLocaleDateString(undefined, {
            month: "short",
            day: "numeric",
          })
        : "";
      setSuccessMsg(
        `Generated ${res?.generated ?? 0} transaction(s) ` +
          `(${res?.settled ?? 0} settled, ${res?.pending ?? 0} pending)` +
          (through ? ` through ${through}.` : ".")
      );
      await reload();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  const activeItems = items.filter((r) => r.is_active);
  const pausedItems = items.filter((r) => !r.is_active);

  return (
    <AppShell>
      {/* Responsive header: title + HelpAnchor stay together in the
          heading (inline-title variant from PR #242 expects the
          HelpAnchor to be a sibling of the heading text). Generate
          Due is a separate item in the flex row that drops to its
          own row at <sm so the cluster doesn't overflow on mobile.
          Pattern: vertical stack on mobile (flex-col), row +
          space-between at sm+. */}
      <header
        data-testid="recurring-page-header"
        className="mb-8 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between"
      >
        <h1 className={`${pageTitle} mb-0 flex items-start gap-1`}>
          Recurring Transactions
          {/* HelpAnchor sits next to the title (not the button) so it
              follows the inline-title variant contract and gives
              every page the same "title + ?" reading order. Deep-
              links to /docs#recurring. */}
          <HelpAnchor
            section="recurring"
            label="Recurring transactions"
            variant="inline-title"
          />
        </h1>
        <button
          onClick={handleGenerate}
          className={`${btnSecondary} self-start sm:self-auto`}
        >
          Generate this period
        </button>
      </header>

      {error && <div className={`mb-6 ${errorCls}`}>{error}</div>}
      {successMsg && <div className={`mb-6 ${successCls}`}>{successMsg}</div>}

      <p className="mb-6 text-sm text-text-muted">
        Generating fills the current billing cycle with this period&apos;s
        recurring transactions. Items due later in the period appear as pending
        until their date arrives. To create a recurring transaction, add a
        regular transaction from the{" "}
        <Link href="/transactions" className="text-accent hover:text-accent-hover">Transactions</Link>{" "}
        page or the Dashboard and check the &quot;Repeats&quot; option.
      </p>

      {fetching ? (
        <Spinner />
      ) : (
        <div className="space-y-6">
          <RecurringTable
            title="Active"
            storageKey={`${SORT_KEY_RECURRING}:active`}
            items={activeItems}
            emptyLabel="No active recurring transactions."
            onStop={handleStop}
            onDelete={handleDelete}
            testId="recurring-active-table"
          />

          {pausedItems.length > 0 && (
            <RecurringTable
              title="Paused"
              storageKey={`${SORT_KEY_RECURRING}:stopped`}
              items={pausedItems}
              paused
              emptyLabel="No paused recurring transactions."
              onResume={handleResume}
              onDelete={handleDelete}
              testId="recurring-paused-table"
            />
          )}
        </div>
      )}
      <ConfirmModal
        open={confirmStop !== null}
        title="Stop Recurring Transaction"
        message={confirmStop ? `Stop "${confirmStop.description}"?\n\nThis will deactivate the recurring schedule and delete any pending future transactions.\n\nSettled (past) transactions will NOT be affected.` : ""}
        confirmLabel="Stop"
        variant="warning"
        onConfirm={() => { if (confirmStop) { doStop(confirmStop.id, confirmStop.description); } setConfirmStop(null); }}
        onCancel={() => setConfirmStop(null)}
      />
      <ConfirmModal
        open={confirmDeleteId !== null}
        title="Delete Recurring Template"
        message={"Permanently delete this recurring template?\n\nAny remaining pending future transactions will also be removed.\nSettled transactions are preserved."}
        confirmLabel="Delete"
        variant="danger"
        onConfirm={() => { if (confirmDeleteId !== null) { doDelete(confirmDeleteId); } setConfirmDeleteId(null); }}
        onCancel={() => setConfirmDeleteId(null)}
      />
    </AppShell>
  );
}
