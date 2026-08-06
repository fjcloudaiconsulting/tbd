"use client";

/**
 * RecentTransactionsWidget — "Recent Transactions" tile for the custom
 * dashboard canvas.
 *
 * Reads all data from DashboardDataProvider; the widget carries no data
 * itself. JSX is ported verbatim from LegacyDashboard (app/dashboard/page.tsx
 * lines 1185–1335) — do NOT sync the two manually; keep the legacy page as
 * the authoritative copy until the canvas fully replaces it.
 *
 * This is the only MUTATION surface on the custom dashboard (the status-pill
 * toggle PUTs /transactions/{id}). The toggle's refresh ordering lives in the
 * provider's `onToggleTransactionStatus` (faithful to legacy); this widget
 * only surfaces a PUT failure inline.
 *
 * TBD-221: it no longer reads `chartFilter` at all. The cross-tile filter used
 * to switch this tile's rendered source to an in-memory snapshot and hide its
 * pager; it now changes the provider's QUERY, so the widget just renders
 * whatever page came back — filtered or not — and always pages it.
 */
import { useState } from "react";
import Link from "next/link";

import { useDashboard } from "@/components/dashboard/DashboardDataProvider";
import Pagination from "@/components/ui/Pagination";
import { extractErrorMessage } from "@/lib/api";
import { formatAmount } from "@/lib/format";
import { card, cardHeader, cardTitle } from "@/lib/styles";
import type { Transaction } from "@/lib/types";

function transactionHighlightHref(tx: Transaction) {
  // The transactions list filters by `effective_period_date_expr =
  // COALESCE(settled_date, date)`, so a deep link built from `tx.date`
  // misses any row whose settled_date differs from its purchase date —
  // notably every credit-card transaction settling on a later statement
  // close. Use the same coalesce here so the row we want highlighted
  // actually lands inside the queried window.
  const effectiveDate = tx.settled_date ?? tx.date;
  const params = new URLSearchParams({
    account_id: String(tx.account_id),
    transaction_id: String(tx.id),
    date_from: effectiveDate,
    date_to: effectiveDate,
  });

  return `/transactions?${params.toString()}`;
}

export default function RecentTransactionsWidget() {
  const {
    sortedVisibleTxs,
    txTotal,
    page,
    setPage,
    pageSize,
    setPageSize,
    dashSort,
    toggleDashSort,
    canAdd,
    onToggleTransactionStatus,
  } = useDashboard();

  const dashSortField = dashSort.field;
  const dashSortDir = dashSort.dir;

  // Local, non-blocking surface for a failed status toggle (the provider
  // rethrows on PUT failure). Mirrors legacy which set the page error banner.
  const [toggleError, setToggleError] = useState<string | null>(null);

  return (
    // flex-col + h-full so the card fills its grid cell; the rows region below
    // scrolls (flex-1 + overflow) while the header, sort-row and pager stay
    // pinned. This keeps the fixed-size row list contained at ANY cell height
    // instead of overflowing onto the canvas when the table is taller than the
    // cell (and lets a resize show more/fewer rows).
    <div className={`${card} flex h-full flex-col`}>
      <div className={`flex shrink-0 items-center justify-between ${cardHeader}`}>
        <h2 className={cardTitle}>Recent Transactions</h2>
      </div>
      {toggleError && (
        <div
          role="alert"
          className="mx-5 mb-2 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger"
        >
          {toggleError}
        </div>
      )}
      {/* Sortable mini-header. Column order mirrors /transactions:
          Date / Description / Status / Amount. Hidden under sm; mobile
          rows collapse to a two-line layout (see below) where header
          labels are redundant. */}
      <div className="hidden shrink-0 sm:block border-b border-border-subtle px-5 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-text-muted">
        <div className="grid grid-cols-12 items-center gap-3">
          {([
            { field: "date" as const, label: "Date", span: "col-span-2", align: "text-left" },
            { field: "description" as const, label: "Description", span: "col-span-6", align: "text-left" },
            { field: "status" as const, label: "Status", span: "col-span-2", align: "text-center" },
            { field: "amount" as const, label: "Amount", span: "col-span-2", align: "text-right" },
          ]).map((col) => {
            const active = dashSortField === col.field;
            // min-h-[32px] is a deliberate dense-header exception: it
            // clears WCAG 2.5.8 (24px AA floor) without inflating the
            // table header to the 44px primary-control floor. The visible
            // ↑/↓ arrow stays in textContent for sighted users and the
            // columns test; aria-label carries the same state to AT.
            return (
              <button
                key={col.field}
                onClick={() => toggleDashSort(col.field)}
                // "Sort transactions by …" is deliberately distinct from
                // the Spending card's "Sort by …" labels so role-name
                // queries stay unambiguous across the two sortable tables.
                aria-label={
                  active
                    ? `Transactions sorted by ${col.label.toLowerCase()}, ${dashSortDir === "asc" ? "ascending" : "descending"}. Activate to reverse.`
                    : `Sort transactions by ${col.label.toLowerCase()}`
                }
                className={`${col.span} ${col.align} min-h-[32px] rounded-sm hover:text-text-primary transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30`}
              >
                {col.label}{active ? (dashSortDir === "asc" ? " ↑" : " ↓") : ""}
              </button>
            );
          })}
        </div>
      </div>
      <div
        className="min-h-0 flex-1 overflow-y-auto divide-y divide-border-subtle focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
        tabIndex={0}
        aria-label="Recent transactions list"
      >
        {sortedVisibleTxs.map((tx) => {
          // TBD-268: the partner is never in the page after the server
          // collapse, so read its account name off the row itself. Non-null
          // only for a MUTUAL link, so this is the ONE transfer signal the row
          // renders from — amount styling included. The raw
          // `linked_transaction_id` also matches a one-way reconciliation
          // match, which would then render unsigned and in the transfer colour
          // while its subline said plain account · category.
          const isPairedTransfer = tx.linked_account_name != null;
          const [fromAcct, toAcct] = tx.type === "expense"
            ? [tx.account_name, tx.linked_account_name]
            : [tx.linked_account_name, tx.account_name];
          const amountClass = `text-sm font-medium tabular-nums ${isPairedTransfer ? "text-info" : tx.type === "income" ? "text-success" : "text-danger"}`;
          const amountText = `${isPairedTransfer ? "" : tx.type === "income" ? "+" : "-"}${formatAmount(tx.amount)}`;
          const subline = isPairedTransfer ? (
            <>{fromAcct} &rarr; {toAcct}</>
          ) : (
            <>{tx.account_name} &middot; {tx.category_name}</>
          );
          const statusPill = !isPairedTransfer ? (
            <button
              onClick={async () => {
                setToggleError(null);
                try {
                  await onToggleTransactionStatus(tx);
                } catch (err) {
                  setToggleError(extractErrorMessage(err));
                }
              }}
              aria-label={`Mark as ${tx.status === "settled" ? "pending" : "settled"}`}
              aria-pressed={tx.status === "settled"}
              className="inline-flex min-h-[44px] items-center rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
            >
              {/* Outer button carries the WCAG 2.5.8 touch target;
                  inner span matches /transactions' pill visual. */}
              <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${tx.status === "settled" ? "bg-success-dim text-success" : "bg-warning-dim text-warning"}`}>
                {tx.status}
              </span>
            </button>
          ) : null;
          return (
            <div key={tx.id} className="px-5 py-2.5">
              {/* Responsive single-tree row. On sm+, this is a 12-col
                  grid mirroring the header (Date / Description / Status
                  / Amount). Below sm, the wrapper drops to a flex
                  column so we get a two-line layout: line 1 the link
                  (date + description + subline), line 2 the status
                  pill + amount on the right. Single Link/pill node so
                  deep-link tests that match `findByRole("link", ...)`
                  keep working. */}
              <div className="flex flex-col gap-1.5 sm:grid sm:grid-cols-12 sm:items-center sm:gap-3">
                <Link
                  href={transactionHighlightHref(tx)}
                  className="-mx-2 -my-1.5 flex min-w-0 items-center gap-3 rounded-md px-2 py-1.5 transition-colors hover:bg-surface-raised focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent sm:col-span-8 sm:my-0"
                >
                  {/* Date + Settled date. The operator requires the
                      settled date visible wherever a transaction renders,
                      so each row stacks the original date over the settled
                      date (settled date when set, em-dash when still
                      pending / unsettled). MM-DD slice matches the
                      compact recent-list date format. */}
                  <span className="flex w-16 shrink-0 flex-col text-xs tabular-nums text-text-secondary sm:w-auto">
                    <span>{tx.date.slice(5)}</span>
                    <span className="text-[10px] text-text-muted" data-testid={`dash-settled-${tx.id}`}>
                      {tx.settled_date ? tx.settled_date.slice(5) : "—"}
                    </span>
                  </span>
                  <div className="min-w-0">
                    <p className="text-sm text-text-primary truncate">{tx.description}</p>
                    <p className="text-[11px] text-text-secondary truncate">{subline}</p>
                  </div>
                </Link>
                {/* Status + Amount: on desktop these split into their
                    own columns (col-span-2 each). On mobile they share
                    one flex row indented under the description. */}
                <div className="flex items-center justify-between gap-2 pl-[4.75rem] sm:contents sm:pl-0">
                  <div className="sm:col-span-2 sm:flex sm:justify-center">
                    {statusPill}
                  </div>
                  <div className="sm:col-span-2 sm:text-right">
                    <span className={amountClass}>{amountText}</span>
                  </div>
                </div>
              </div>
            </div>
          );
        })}
        {/* Keyed off the RENDERED list, not the raw page array: under a chart
            filter the rendered source is the snapshot, so keying off
            `transactions` could leave the tile with zero rows and no empty
            state — a blank card. */}
        {sortedVisibleTxs.length === 0 && (
          <div className="px-5 py-6 text-center text-sm text-text-muted">
            {!canAdd ? "Create accounts and categories first." : "No transactions this period."}
          </div>
        )}
      </div>
      {/* TBD-221: the pager no longer hides under a chart filter. The
          drilldown is a paginated SERVER query now, so hiding it would cap
          the slice's list at one page while its own total said otherwise —
          the same class of silent truncation this ticket removed. */}
      {txTotal > 0 && (
        <div className="shrink-0 border-t border-border px-5">
          {/* Page-size selector (10–100) lets the user fill a resized card with
              more rows instead of leaving blank space below a fixed 10. Options
              default to PAGE_SIZE_OPTIONS = [10, 25, 50, 100]. */}
          <Pagination
            page={page + 1}
            pageSize={pageSize}
            total={txTotal}
            onPageChange={(n) => setPage(n - 1)}
            onPageSizeChange={setPageSize}
          />
        </div>
      )}
    </div>
  );
}
