"use client";

/**
 * Manual batch transaction entry (L3.2 Wave 2A).
 *
 * Spreadsheet-style grid (5 default empty rows) where the user types a
 * handful of receipts at once and submits as a single request. The
 * backend processes each row in its own savepoint so partial-success
 * is the contract: surviving rows commit, failing rows surface a
 * per-row error icon next to the row.
 *
 * UX:
 *  - Default state: 5 empty rows (matches the spec's "5-10 visible"
 *    target). "+ Add row" appends; per-row trash icon removes (down to
 *    a floor of 1 row, since min_length=1 on the request shape).
 *  - Keyboard nav: Tab moves to the next cell. Pressing Enter inside
 *    the last field of the last row appends a new row and focuses
 *    its first field. Shift+Tab walks backward.
 *  - Validation is client-side first (each cell must be filled before
 *    submit), then server-side per-row on submit. Per-row errors
 *    render inline beside the row's status column.
 *  - Submit is disabled while any row is missing a required cell,
 *    while the request is in flight, and after submit until the user
 *    resets the form or navigates away. We do NOT auto-retry — partial
 *    success means the user can clear the bad rows and submit again.
 */

import {
  KeyboardEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import Link from "next/link";
import AppShell from "@/components/AppShell";
import CategorySelect from "@/components/ui/CategorySelect";
import DescriptionAutocomplete from "@/components/transactions/DescriptionAutocomplete";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { todayISO } from "@/lib/format";
import {
  btnPrimary,
  btnSecondary,
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
  input,
  pageTitle,
  success as successCls,
} from "@/lib/styles";

// `label` (in lib/styles) bakes in `display: block` + `mb-1.5` so it can
// sit on top of form inputs in stacked single-transaction forms. Apply
// it to a `<th>` and the cell collapses out of the table row, dragging
// every header into a vertical stack while the `<td>`s flow as normal
// table cells (root cause of the 2026-05-13 layout regression).
// `thLabel` keeps the same typography but lets the browser default
// `display: table-cell` win.
const thLabel =
  "text-xs font-semibold uppercase tracking-[0.08em] text-text-muted";
import type {
  Account,
  BatchTransactionRowInput,
  BatchTransactionsResponse,
  Category,
} from "@/lib/types";

const DEFAULT_ROW_COUNT = 5;
const MAX_ROWS = 500;

// Stable client-side row identity. ``row_number`` shipped to the API is
// derived from array index on submit (1-based), but inside the form we
// need a non-changing key so React doesn't lose focus when the user
// removes a row above the cursor.
type DraftStatus = "idle" | "ok" | "error";
type TxStatus = "settled" | "pending";

interface DraftRow {
  key: string;
  date: string;
  description: string;
  amount: string;
  type: "expense" | "income";
  account_id: number | "";
  category_id: number | "";
  tx_status: TxStatus;
  status: DraftStatus;
  errorMessage?: string;
}

function blankRow(): DraftRow {
  return {
    key: crypto.randomUUID(),
    date: todayISO(),
    description: "",
    amount: "",
    type: "expense",
    account_id: "",
    category_id: "",
    tx_status: "settled",
    status: "idle",
  };
}

function rowIsBlank(row: DraftRow): boolean {
  return (
    !row.description.trim() &&
    !row.amount.trim() &&
    row.account_id === "" &&
    row.category_id === ""
  );
}

function rowIsValid(row: DraftRow): boolean {
  if (!row.date) return false;
  if (!row.description.trim()) return false;
  if (!row.amount.trim()) return false;
  const amt = Number(row.amount);
  if (!Number.isFinite(amt) || amt <= 0) return false;
  if (row.account_id === "") return false;
  if (row.category_id === "") return false;
  return true;
}

export default function BatchEntryPage() {
  const [rows, setRows] = useState<DraftRow[]>(() =>
    Array.from({ length: DEFAULT_ROW_COUNT }, blankRow),
  );
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [topError, setTopError] = useState("");
  const [summary, setSummary] = useState<{
    imported: number;
    errored: number;
  } | null>(null);

  // Ref for the first cell of the most-recently-added row so we can
  // focus it after "+ Add row" / Enter-to-extend.
  const newRowFocusRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const [acctList, catList] = await Promise.all([
          apiFetch<Account[]>("/api/v1/accounts"),
          apiFetch<Category[]>("/api/v1/categories"),
        ]);
        setAccounts(acctList.filter((a) => a.is_active));
        setCategories(catList);
      } catch (err) {
        setTopError(extractErrorMessage(err, "Failed to load accounts"));
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const populatedRows = useMemo(
    () => rows.filter((r) => !rowIsBlank(r)),
    [rows],
  );

  const submitEnabled = useMemo(() => {
    if (submitting) return false;
    if (populatedRows.length === 0) return false;
    return populatedRows.every(rowIsValid);
  }, [populatedRows, submitting]);

  function updateRow(idx: number, patch: Partial<DraftRow>) {
    setRows((prev) => {
      const next = [...prev];
      next[idx] = { ...next[idx], ...patch, status: "idle", errorMessage: undefined };
      return next;
    });
  }

  function addRow() {
    if (rows.length >= MAX_ROWS) return;
    const fresh = blankRow();
    setRows((prev) => [...prev, fresh]);
    // Focus the new row's first cell on the next tick.
    setTimeout(() => {
      newRowFocusRef.current?.focus();
    }, 0);
  }

  function removeRow(idx: number) {
    setRows((prev) => {
      if (prev.length <= 1) return prev;
      return prev.filter((_, i) => i !== idx);
    });
  }

  function handleRowKeyDown(
    e: KeyboardEvent<HTMLInputElement | HTMLSelectElement>,
    idx: number,
    isLastCell: boolean,
  ) {
    // Enter on the last cell of the last row adds a new row and moves
    // focus to its first field. This mirrors spreadsheet ergonomics.
    if (e.key === "Enter" && isLastCell && idx === rows.length - 1) {
      e.preventDefault();
      addRow();
    }
  }

  async function handleSubmit() {
    setTopError("");
    setSummary(null);
    if (populatedRows.length === 0) return;

    // Map populated rows to API payload. row_number is 1-based and
    // unique per-row across the request (the validator on
    // BatchTransactionsRequest rejects duplicates with 422).
    const payload: { rows: BatchTransactionRowInput[] } = {
      rows: populatedRows.map((row, i) => ({
        row_number: i + 1,
        transaction: {
          account_id: row.account_id as number,
          category_id: row.category_id as number,
          description: row.description.trim(),
          amount: row.amount,
          type: row.type,
          date: row.date,
          status: row.tx_status,
        },
      })),
    };

    setSubmitting(true);
    try {
      const resp = await apiFetch<BatchTransactionsResponse>(
        "/api/v1/transactions/batch",
        {
          method: "POST",
          body: JSON.stringify(payload),
        },
      );
      // Splat results back onto the populated rows by row_number so
      // success/error icons land on the right grid row.
      setRows((prev) => {
        const next = [...prev];
        // Build (populated index → row_number) map.
        const populatedKeys = populatedRows.map((r) => r.key);
        const okSet = new Set(resp.results.map((r) => r.row_number));
        const errMap = new Map(
          resp.errors.map((e) => [e.row_number, e.error]),
        );
        for (const [key, rowNumber] of populatedKeys.map(
          (k, i) => [k, i + 1] as const,
        )) {
          const realIdx = next.findIndex((r) => r.key === key);
          if (realIdx < 0) continue;
          if (okSet.has(rowNumber)) {
            next[realIdx] = { ...next[realIdx], status: "ok", errorMessage: undefined };
          } else if (errMap.has(rowNumber)) {
            next[realIdx] = {
              ...next[realIdx],
              status: "error",
              errorMessage: errMap.get(rowNumber)!,
            };
          }
        }
        return next;
      });
      setSummary({ imported: resp.imported_count, errored: resp.error_count });
    } catch (err) {
      setTopError(extractErrorMessage(err, "Batch submit failed"));
    } finally {
      setSubmitting(false);
    }
  }

  function resetForm() {
    setRows(Array.from({ length: DEFAULT_ROW_COUNT }, blankRow));
    setSummary(null);
    setTopError("");
  }

  // Render the inputs for a single row once and reuse them across the
  // desktop `<table>` cells and the mobile labeled-card layout. Handlers
  // close over `updateRow`, `accounts`, `categories`, `newRowFocusRef`
  // from the enclosing component so the two layouts stay byte-for-byte
  // in sync with state, ARIA, and focus management.
  function renderRowFields(row: DraftRow, idx: number, isNewest: boolean) {
    return {
      date: (
        <input
          type="date"
          className={input}
          value={row.date}
          onChange={(e) => updateRow(idx, { date: e.target.value })}
          aria-label={`Row ${idx + 1} date`}
          ref={isNewest ? newRowFocusRef : undefined}
        />
      ),
      description: (
        <DescriptionAutocomplete
          id={`row-${row.key}-description`}
          type={row.type}
          value={row.description}
          onChange={(next) => updateRow(idx, { description: next })}
          onPick={(s) => {
            // Pre-fill the category only when the row's category is still
            // empty, mirroring the single-transaction add form. We never
            // overwrite a user-chosen category.
            if (row.category_id === "") {
              updateRow(idx, {
                description: s.description,
                category_id: s.category_id,
              });
            }
          }}
          placeholder="e.g. Coffee shop"
          ariaLabel={`Row ${idx + 1} description`}
        />
      ),
      amount: (
        <input
          type="number"
          step="0.01"
          min="0.01"
          className={input}
          value={row.amount}
          placeholder="0.00"
          onChange={(e) => updateRow(idx, { amount: e.target.value })}
          aria-label={`Row ${idx + 1} amount`}
        />
      ),
      type: (
        <select
          className={input}
          value={row.type}
          onChange={(e) =>
            updateRow(idx, {
              type: e.target.value as "expense" | "income",
              // Type change invalidates current category; clear it.
              category_id: "",
            })
          }
          aria-label={`Row ${idx + 1} type`}
        >
          <option value="expense">Expense</option>
          <option value="income">Income</option>
        </select>
      ),
      account: (
        <select
          className={input}
          value={row.account_id === "" ? "" : String(row.account_id)}
          onChange={(e) =>
            updateRow(idx, {
              account_id:
                e.target.value === "" ? "" : Number(e.target.value),
            })
          }
          aria-label={`Row ${idx + 1} account`}
        >
          <option value="">Pick account…</option>
          {accounts.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </select>
      ),
      category: (
        <CategorySelect
          id={`row-${row.key}-category`}
          categories={categories}
          value={row.category_id}
          onChange={(cid) => updateRow(idx, { category_id: cid })}
          filterType={row.type}
          aria-label={`Row ${idx + 1} category`}
        />
      ),
      status: (
        <select
          className={input}
          value={row.tx_status}
          onChange={(e) =>
            updateRow(idx, { tx_status: e.target.value as TxStatus })
          }
          aria-label={`Row ${idx + 1} status`}
        >
          <option value="settled">Settled</option>
          <option value="pending">Pending</option>
        </select>
      ),
      result: (
        <>
          {row.status === "ok" && (
            <span
              className="inline-flex items-center gap-1 text-success"
              aria-label={`Row ${idx + 1} imported`}
            >
              <span aria-hidden>✓</span> Imported
            </span>
          )}
          {row.status === "error" && (
            <span
              className="inline-flex items-start gap-1 text-danger"
              role="alert"
            >
              <span aria-hidden>✕</span>
              <span className="text-xs">{row.errorMessage}</span>
            </span>
          )}
          {row.status === "idle" && !rowIsBlank(row) &&
            (rowIsValid(row) ? (
              <span className="text-xs text-text-muted">Ready</span>
            ) : (
              <span className="text-xs text-warning">Fill all cells</span>
            ))}
        </>
      ),
    };
  }

  return (
    <AppShell>
      <div className="mb-8 flex items-center justify-between">
        <h1 className={`${pageTitle} mb-0`}>Batch entry</h1>
        <div className="flex items-center gap-2">
          <Link href="/transactions" className={btnSecondary}>
            Back to transactions
          </Link>
        </div>
      </div>

      <p className="mb-6 max-w-2xl text-sm text-text-muted">
        Type a handful of receipts at once. Each row commits independently,
        so a typo in one row never blocks the rest of the batch. Press Tab
        to move between cells. Press Enter on the last cell of the last
        row to add another row.
      </p>

      {topError && <div className={`mb-6 ${errorCls}`}>{topError}</div>}

      {summary && (
        <div
          className={`mb-6 ${summary.errored === 0 ? successCls : errorCls}`}
          role="status"
          aria-live="polite"
        >
          {summary.imported} row{summary.imported === 1 ? "" : "s"} imported
          {summary.errored > 0 && (
            <>
              , {summary.errored} row{summary.errored === 1 ? "" : "s"}{" "}
              failed. See the per-row messages below.
            </>
          )}
          .
        </div>
      )}

      <div className={`${card} overflow-hidden`}>
        <div className={`${cardHeader} flex items-center justify-between`}>
          <h2 className={cardTitle}>
            <span aria-live="polite">
              {populatedRows.length} of {rows.length} row
              {rows.length === 1 ? "" : "s"} filled
            </span>
          </h2>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={addRow}
              className={btnSecondary}
              disabled={rows.length >= MAX_ROWS}
            >
              + Add row
            </button>
          </div>
        </div>

        {loading ? (
          <div className="p-6 text-sm text-text-muted">Loading…</div>
        ) : accounts.length === 0 ? (
          <div className="p-6 text-sm text-text-muted">
            No active accounts. Add an account first from{" "}
            <Link href="/accounts" className="underline">
              Accounts
            </Link>
            .
          </div>
        ) : (
          <div className="overflow-x-auto md:overflow-x-auto">
            {/* Single `<table>` source of truth. On md+ it renders as a
                spreadsheet. Under md it falls back to one stacked
                labeled card per row via the `batch-grid` CSS rules in
                `app/globals.css` (each `<td>` exposes its column name
                through `data-label`, the header row hides, and rows
                turn into vertical mini-forms). Keeping a single DOM
                tree avoids the dup-aria-label trap that the earlier
                desktop/mobile split surfaced in tests. */}
            <table
              className="batch-grid w-full text-sm"
              role="grid"
              aria-label="Batch entry grid"
            >
              <thead className="batch-grid__head">
                <tr className="border-b border-border text-left">
                  <th scope="col" className="w-10 px-2 py-2 text-xs text-text-muted">#</th>
                  <th scope="col" className={`${thLabel} w-32 px-2 py-2`}>Date</th>
                  <th scope="col" className={`${thLabel} w-64 px-2 py-2`}>Description</th>
                  <th scope="col" className={`${thLabel} w-28 px-2 py-2`}>Amount</th>
                  <th scope="col" className={`${thLabel} w-28 px-2 py-2`}>Type</th>
                  <th scope="col" className={`${thLabel} w-44 px-2 py-2`}>Account</th>
                  <th scope="col" className={`${thLabel} w-44 px-2 py-2`}>Category</th>
                  <th scope="col" className={`${thLabel} w-32 px-2 py-2`}>Status</th>
                  <th scope="col" className={`${thLabel} w-32 px-2 py-2`}>Result</th>
                  <th scope="col" className="w-10 px-2 py-2"><span className="sr-only">Remove</span></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row, idx) => {
                  const isLast = idx === rows.length - 1;
                  const isNewest =
                    idx === rows.length - 1 && rows.length > DEFAULT_ROW_COUNT;
                  const fields = renderRowFields(row, idx, isNewest);
                  return (
                    <tr
                      key={row.key}
                      className="batch-grid__row border-b border-border last:border-b-0"
                      data-row-index={idx}
                    >
                      <td className="px-2 py-2 text-xs text-text-muted" data-label="Row">
                        {idx + 1}
                      </td>
                      <td className="px-2 py-2" data-label="Date">{fields.date}</td>
                      <td className="px-2 py-2" data-label="Description">{fields.description}</td>
                      <td className="px-2 py-2" data-label="Amount">{fields.amount}</td>
                      <td className="px-2 py-2" data-label="Type">{fields.type}</td>
                      <td className="px-2 py-2" data-label="Account">{fields.account}</td>
                      <td className="px-2 py-2" data-label="Category">{fields.category}</td>
                      <td className="px-2 py-2" data-label="Status">{fields.status}</td>
                      <td className="px-2 py-2" data-label="Result">{fields.result}</td>
                      <td className="px-2 py-2 batch-grid__remove">
                        <button
                          type="button"
                          onClick={() => removeRow(idx)}
                          className="text-text-muted hover:text-danger"
                          aria-label={`Remove row ${idx + 1}`}
                          disabled={rows.length <= 1}
                          onKeyDown={(e) =>
                            handleRowKeyDown(
                              e as unknown as KeyboardEvent<HTMLInputElement>,
                              idx,
                              isLast,
                            )
                          }
                        >
                          ✕
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="mt-6 flex items-center justify-end gap-2">
        {summary && (
          <button type="button" onClick={resetForm} className={btnSecondary}>
            Clear and start over
          </button>
        )}
        <button
          type="button"
          onClick={handleSubmit}
          className={btnPrimary}
          disabled={!submitEnabled}
        >
          {submitting
            ? "Submitting…"
            : `Submit ${populatedRows.length || ""} row${
                populatedRows.length === 1 ? "" : "s"
              }`.trim()}
        </button>
      </div>
    </AppShell>
  );
}
