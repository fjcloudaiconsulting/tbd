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
import { ApiResponseError, apiFetch, extractErrorMessage } from "@/lib/api";

import { demotionNotice } from "@/lib/demotion";
import {
  useTableState,
  paginate,
  pageCount,
  type SortDir,
} from "@/lib/hooks/use-table-state";
import { SORT_KEY_RECURRING } from "@/lib/hooks/persisted-keys";
import { btnSecondary, card, cardHeader, cardTitle, error as errorCls, input as inputCls, label as labelCls, success as successCls, pageTitle } from "@/lib/styles";
import type { RecurringTransaction } from "@/lib/types";
import { instalmentDone, seriesRunning } from "@/lib/recurring";
import { useMoney } from "@/lib/hooks/use-org-currency";

const FREQ_LABELS: Record<string, string> = {
  weekly: "Weekly",
  biweekly: "Every 2 weeks",
  monthly: "Monthly",
  quarterly: "Quarterly",
  yearly: "Yearly",
};

// Instalment progress, e.g. "3 of 12" (TBD-275). Null for an open-ended
// series -- `occurrence_count == null` is the overwhelming majority (every
// template predating this feature) and must render NOTHING, not "0 of 0".
//
// The remainder is deliberately not on the wire; the client subtracts. It can
// legitimately be NEGATIVE (the user shortened a plan below what it had
// already delivered), so this renders "12 of 8" rather than a clamped lie --
// and `instalmentDone` still reports the series finished, which it is.
function instalmentLabel(r: RecurringTransaction): string | null {
  if (r.occurrence_count == null) return null;
  return `${r.occurrences_elapsed} of ${r.occurrence_count}`;
}

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
// `factor` is +1 for ascending, -1 for descending. It is applied ONLY to the
// value-vs-value comparison so that the null/empty sentinel (always last) is
// never flipped by the direction multiplier.
function cmpString(
  a: string | null | undefined,
  b: string | null | undefined,
  factor: 1 | -1,
): number {
  const aEmpty = a == null || a === "";
  const bEmpty = b == null || b === "";
  if (aEmpty && bEmpty) return 0;
  if (aEmpty) return 1;  // empty always after non-empty, direction-independent
  if (bEmpty) return -1;
  return factor * a!.localeCompare(b!, undefined, { sensitivity: "base" });
}

function cmpNumber(a: number, b: number, factor: 1 | -1): number {
  return factor * (a - b);
}

function sortRecurring(
  rows: RecurringTransaction[],
  field: SortField,
  dir: SortDir,
): RecurringTransaction[] {
  const factor: 1 | -1 = dir === "asc" ? 1 : -1;
  const sorted = [...rows].sort((a, b) => {
    switch (field) {
      case "description":
        return cmpString(a.description, b.description, factor);
      case "account":
        return cmpString(a.account_name, b.account_name, factor);
      case "category":
        return cmpString(a.category_name, b.category_name, factor);
      case "frequency":
        return cmpString(
          FREQ_LABELS[a.frequency] ?? a.frequency,
          FREQ_LABELS[b.frequency] ?? b.frequency,
          factor,
        );
      case "next_due_date":
        // ISO date strings (YYYY-MM-DD) sort chronologically as strings.
        return cmpString(a.next_due_date, b.next_due_date, factor);
      case "amount":
        return cmpNumber(Number(a.amount), Number(b.amount), factor);
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
  // TBD-272/273. Passed to the Active table only.
  onSkipNext?: (item: RecurringTransaction) => void;
  onEditNext?: (item: RecurringTransaction) => void;
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
  onSkipNext,
  onEditNext,
  onDelete,
  testId,
}: RecurringTableProps) {
  const money = useMoney();
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
  const totalPages = pageCount(sorted.length, pageSize);
  const safePage = Math.min(page, totalPages);
  const pageRows = useMemo(
    () => paginate(sorted, safePage, pageSize),
    [sorted, safePage, pageSize],
  );
  const showPagination = totalPages > 1;

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
                  {/* Outline, not a fill: the row uses hover:bg-surface-raised,
                      so a filled badge would vanish on hover. Quiet-by-default
                      (docs/product/PRODUCT.md) -- progress is data, not an alert. */}
                  {instalmentLabel(r) && (
                    <span
                      // "3 of 12" next to a description reads as an unlabelled
                      // fragment: the visual context that makes it mean
                      // "instalment progress" is not conveyed to a screen
                      // reader. WCAG 2.2 AA is a product commitment
                      // (docs/product/PRODUCT.md).
                      aria-label={`instalment ${instalmentLabel(r)}`}
                      className={`ml-1.5 rounded border px-1.5 py-0.5 text-[10px] font-medium tabular-nums ${
                        instalmentDone(r)
                          ? "border-border-subtle text-text-muted"
                          : "border-border text-text-secondary"
                      }`}
                    >
                      {instalmentLabel(r)}
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
                  {money(r.amount)}
                </td>
                <td className="px-3 py-3">
                  <span className="flex flex-wrap justify-end gap-x-2 gap-y-1">
                    {/* TBD-272/273. Only for a series that can still deliver an
                        occurrence; the server's 409 decides everything else. */}
                    {onEditNext && onSkipNext && seriesRunning(r) && (
                      <>
                        <button
                          onClick={() => onEditNext(r)}
                          aria-label={`Edit next amount: ${r.description}`}
                          className="min-h-[44px] md:min-h-0 whitespace-nowrap text-xs text-text-muted hover:text-accent"
                        >
                          Edit next
                        </button>
                        <button
                          onClick={() => onSkipNext(r)}
                          aria-label={`Skip next: ${r.description}`}
                          className="min-h-[44px] md:min-h-0 whitespace-nowrap text-xs text-text-muted hover:text-accent"
                        >
                          Skip next
                        </button>
                      </>
                    )}
                    {paused ? (
                      <button
                        onClick={() => onResume?.(r)}
                        className="min-h-[44px] md:min-h-0 text-xs text-text-muted hover:text-accent"
                      >
                        Resume
                      </button>
                    ) : (
                      <button
                        onClick={() => onStop?.(r)}
                        className="min-h-[44px] md:min-h-0 text-xs text-text-muted hover:text-accent"
                      >
                        Stop
                      </button>
                    )}
                    <button
                      onClick={() => onDelete(r.id)}
                      className="min-h-[44px] md:min-h-0 text-xs text-text-muted hover:text-danger"
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
                  {instalmentLabel(r) && (
                    <>
                      {" "}
                      &middot;{" "}
                      <span aria-label={`instalment ${instalmentLabel(r)}`}>
                        {instalmentLabel(r)}
                      </span>
                    </>
                  )}
                </div>
              </div>
              <div
                className={`shrink-0 text-right text-sm font-semibold tabular-nums ${r.type === "income" ? "text-success" : "text-danger"}`}
              >
                {r.type === "income" ? "+" : "-"}
                {money(r.amount)}
              </div>
            </div>
            {r.category_name && (
              <div className="text-xs text-text-secondary truncate">{r.category_name}</div>
            )}
            <div className="text-xs text-text-muted">{FREQ_LABELS[r.frequency] ?? r.frequency}</div>
            <div className="flex flex-wrap gap-2 pt-2 border-t border-border-subtle">
              {onEditNext && onSkipNext && seriesRunning(r) && (
                <>
                  <button
                    onClick={() => onEditNext(r)}
                    aria-label={`Edit next amount: ${r.description}`}
                    className="min-h-[44px] px-3 rounded-md border border-border text-sm text-text-secondary"
                  >
                    Edit next
                  </button>
                  <button
                    onClick={() => onSkipNext(r)}
                    aria-label={`Skip next: ${r.description}`}
                    className="min-h-[44px] px-3 rounded-md border border-border text-sm text-text-secondary"
                  >
                    Skip next
                  </button>
                </>
              )}
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
            page={safePage}
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
  // TBD-272/273.
  const [confirmSkip, setConfirmSkip] = useState<RecurringTransaction | null>(null);
  const [editNext, setEditNext] = useState<{ item: RecurringTransaction; amount: string } | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const money = useMoney();
  const signed = (r: RecurringTransaction, value: number | string) =>
    `${r.type === "income" ? "+" : "-"}${money(value)}`;

  const reload = useCallback(async () => {
    const data = await apiFetch<RecurringTransaction[]>("/api/v1/recurring");
    setItems(data ?? []);
    setFetching(false);
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial data fetch: reload() writes the recurring templates list into state once auth resolves
    if (!loading && user) reload().catch(() => setFetching(false));
  }, [loading, user, reload]);

  async function handleStop(item: RecurringTransaction) {
    setConfirmStop({ id: item.id, description: item.description });
  }

  async function doStop(id: number, description: string) {
    setError(""); setSuccessMsg("");
    try {
      const res = await apiFetch<{ pending_removed: number; demoted_ids?: number[] }>(`/api/v1/recurring/${id}/stop`, { method: "POST" });
      // TBD-312. Stopping a template deletes its pending rows, and deleting a
      // row that another row was matched against marks that other row
      // REJECTED, irreversibly. Reported in the same words the transactions
      // page uses for the same server-side act; `demotionNotice` returns ""
      // when nothing was demoted, so the filter keeps the message clean.
      setSuccessMsg([
        `Stopped "${description}". ${res?.pending_removed ?? 0} pending transaction(s) removed.`,
        demotionNotice(res?.demoted_ids ?? []),
      ].filter(Boolean).join(" "));
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
      const res = await apiFetch<{ pending_removed: number; demoted_ids?: number[] }>(`/api/v1/recurring/${id}`, { method: "DELETE" });
      // TBD-312 -- see doStop. Fenced separately; both routes reach the
      // demotion by different paths.
      setSuccessMsg([
        `Deleted. ${res?.pending_removed ?? 0} pending transaction(s) removed.`,
        demotionNotice(res?.demoted_ids ?? []),
      ].filter(Boolean).join(" "));
      await reload();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  // TBD-272. The date is the row's frontier, never today: the server 409s when
  // it no longer matches, so a stale screen cannot skip the wrong occurrence.
  async function doSkipNext(item: RecurringTransaction) {
    setError(""); setSuccessMsg("");
    setSubmitting(true);
    try {
      await apiFetch(`/api/v1/recurring/${item.id}/skip-next`, {
        method: "POST",
        body: JSON.stringify({ occurrence_date: item.next_due_date }),
      });
      setSuccessMsg(`Skipped "${item.description}" on ${item.next_due_date}.`);
    } catch (err) { setError(extractErrorMessage(err)); }
    await reload().catch(() => {});
    setSubmitting(false);
    setConfirmSkip(null);
  }

  // Checked before the irreversible create. The input's own min/step are not
  // enforced on a typed value; the server takes 12 digits with 2 decimals.
  const editNextError = editNext === null
    ? null
    : /^\d{1,10}(\.\d{1,2})?$/.test(editNext.amount) && Number(editNext.amount) > 0
      ? null
      : Number(editNext.amount) > 0
        ? "Enter an amount with up to 10 digits and 2 decimals."
        : "Enter an amount above 0.";
  const editNextValid = editNext !== null && editNextError === null;
  // Amounts are Decimal strings on the wire ("1200.00"): compare as numbers.
  const editNextUnchanged = editNext !== null && Number(editNext.amount) === Number(editNext.item.amount);

  // TBD-273. Write the occurrence, then edit its amount. Never retry the
  // materialise: it moved the frontier, so a retry would 409 or take the NEXT one.
  async function doEditNext(item: RecurringTransaction, amount: string) {
    setError(""); setSuccessMsg("");
    setSubmitting(true);
    let created: { id: number } | null = null;
    try {
      created = await apiFetch<{ id: number }>(`/api/v1/recurring/${item.id}/materialise-next`, {
        method: "POST",
        body: JSON.stringify({ occurrence_date: item.next_due_date }),
      });
      await apiFetch(`/api/v1/transactions/${created.id}`, {
        method: "PUT",
        body: JSON.stringify({ amount }),
      });
      setSuccessMsg(`"${item.description}" on ${item.next_due_date} is now ${signed(item, amount)}. Later occurrences stay at ${signed(item, item.amount)}.`);
    } catch (err) {
      const msg = extractErrorMessage(err);
      setError(created
        ? `The ${item.next_due_date} occurrence was created at ${signed(item, item.amount)}, but the new amount didn't save: ${msg} Edit it on the Transactions page.`
        : err instanceof ApiResponseError
          ? msg
          // No HTTP response: the row may exist. A retry is safe (the server 409s).
          : `${msg}${/[.!?]$/.test(msg) ? "" : "."} Refresh to check whether it was created.`);
    }
    await reload().catch(() => {});
    setSubmitting(false);
    setEditNext(null);
  }

  async function handleGenerate() {
    setError(""); setSuccessMsg("");
    try {
      const res = await apiFetch<{
        generated: number; settled: number; pending: number; backfilled?: number; period_end: string;
      }>("/api/v1/recurring/generate", { method: "POST" });
      const through = res?.period_end
        ? new Date(`${res.period_end}T00:00:00`).toLocaleDateString(undefined, {
            month: "short",
            day: "numeric",
          })
        : "";
      // TBD-285: catch-up keeps back-dated rows on their real dates; say so.
      const backfilled = res?.backfilled ?? 0;
      setSuccessMsg(
        `Generated ${res?.generated ?? 0} transaction(s) ` +
          `(${res?.settled ?? 0} settled, ${res?.pending ?? 0} pending)` +
          (through ? ` through ${through}.` : ".") +
          (backfilled > 0
            ? ` ${backfilled} of them ${backfilled === 1 ? "is" : "are"} dated before the current billing cycle.`
            : "")
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
            onSkipNext={setConfirmSkip}
            onEditNext={(item) => setEditNext({ item, amount: String(item.amount) })}
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
        open={confirmSkip !== null}
        title="Skip Next Occurrence"
        message={confirmSkip
          ? `Skip "${confirmSkip.description}" on ${confirmSkip.next_due_date} (${signed(confirmSkip, confirmSkip.amount)})?\n\nIt will stay on your Transactions page marked Excluded and won't be counted in balances or reports. Later occurrences are unchanged.` +
            (confirmSkip.occurrence_count != null ? `\n\nIt still counts as 1 of the ${confirmSkip.occurrence_count} occurrences.` : "") +
            "\n\nThis can't be undone."
          : ""}
        confirmLabel="Skip"
        variant="warning"
        submitting={submitting}
        onConfirm={() => { if (confirmSkip) doSkipNext(confirmSkip); }}
        onCancel={() => setConfirmSkip(null)}
      />
      <ConfirmModal
        open={editNext !== null}
        title="Edit Next Amount"
        message={editNext
          ? `Change the amount of "${editNext.item.description}" on ${editNext.item.next_due_date} only. The series stays at ${signed(editNext.item, editNext.item.amount)}.`
          : ""}
        confirmLabel="Save amount"
        submitting={submitting}
        confirmDisabled={!editNextValid || editNextUnchanged}
        onConfirm={() => {
          if (editNext && editNextValid && !editNextUnchanged) {
            doEditNext(editNext.item, editNext.amount);
          }
        }}
        onCancel={() => setEditNext(null)}
      >
        {editNext && (
          <div className="mt-4">
            <label htmlFor="edit-next-amount" className={labelCls}>
              Amount for {editNext.item.next_due_date}
            </label>
            <input
              id="edit-next-amount"
              type="number"
              step="0.01"
              min="0.01"
              inputMode="decimal"
              autoFocus
              value={editNext.amount}
              onChange={(e) => setEditNext({ ...editNext, amount: e.target.value })}
              aria-invalid={!editNextValid}
              aria-describedby={editNextError ? "edit-next-amount-error" : undefined}
              className={inputCls}
            />
            {editNextError && (
              <p id="edit-next-amount-error" className="mt-1 text-xs text-danger">
                {editNextError}
              </p>
            )}
          </div>
        )}
      </ConfirmModal>
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
