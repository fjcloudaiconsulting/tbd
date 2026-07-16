"use client";

/**
 * Per-widget filter editor (date / accounts / categories / txn_type /
 * status / amount_range / tags).
 *
 * Phase 4b: ``date_range`` is the ONLY canvas-shared field, so it is the
 * only field that can carry the "Overrides canvas" pill — the pill fires
 * when the widget date DIFFERS from the canvas date, via
 * ``isFieldOverridden`` from ``lib/reports/resolve`` (not reimplemented).
 * Accounts, categories, txn_type, amount_range and tags are all
 * widget-only now (the canvas can't hold them), so they NEVER show the
 * override pill — they're plain per-widget controls.
 */
import { useEffect } from "react";

import AccountFilter from "@/components/reports/filters/AccountFilter";
import AmountRangeFilter from "@/components/reports/filters/AmountRangeFilter";
import CategoryPicker from "@/components/reports/filters/CategoryPicker";
import DatePresetChips from "@/components/reports/filters/DatePresetChips";
import StatusFilter from "@/components/reports/filters/StatusFilter";
import TagFilter from "@/components/reports/filters/TagFilter";
import { asTxnTypeArray, isFieldOverridden } from "@/lib/reports/resolve";
import type {
  CanvasFilters,
  Dataset,
  TagMatch,
  TxnType,
  WidgetFilters,
} from "@/lib/reports/types";

function OverridePill() {
  return (
    <span
      data-testid="override-pill"
      className="ml-2 inline-flex items-center rounded-full bg-accent/15 px-2 py-0.5 text-[10px] font-medium text-accent"
    >
      Overrides canvas
    </span>
  );
}

export default function FilterEditor({
  filters,
  canvasFilters,
  dataset,
  hideTxnType = false,
  onChange,
}: {
  filters: WidgetFilters;
  canvasFilters: CanvasFilters;
  /**
   * The widget's data source. ``transfer`` is a transactions-only
   * concept (``recurring`` is income/expense only, ``accounts`` has no
   * txn_type), so the Type control only offers Transfer when the
   * source is ``transactions`` — otherwise the backend 422s the choice.
   */
  dataset: Dataset;
  /**
   * When true, hides the transaction-type checkboxes entirely. Used for
   * widget types where txn_type is a backend no-op (e.g. Sankey), so the
   * user is not shown a control that has no effect on the chart.
   */
  hideTxnType?: boolean;
  onChange: (next: WidgetFilters) => void;
}) {
  const allowTransfer = dataset === "transactions";
  return (
    <div className="flex flex-col gap-4 rounded-md border border-border bg-bg p-3">
      <div className="text-[11px] font-medium uppercase tracking-wider text-text-muted">
        Filters (this widget)
      </div>

      <div className="flex flex-col gap-1">
        <div className="flex items-center text-xs text-text-secondary">
          Date range
          {isFieldOverridden("date_range", filters, canvasFilters) && (
            <OverridePill />
          )}
        </div>
        <DatePresetChips
          value={filters.date_range}
          ariaPrefix="Widget"
          onChange={(next) =>
            onChange({
              ...filters,
              date_range: next || undefined,
            })
          }
        />
      </div>

      <div className="flex flex-col gap-1">
        <div className="flex items-center text-xs text-text-secondary">
          Accounts
        </div>
        <AccountFilter
          value={filters.account_ids ?? []}
          ariaPrefix="Widget account"
          label=""
          onChange={(account_ids) =>
            onChange({
              ...filters,
              account_ids: account_ids.length > 0 ? account_ids : undefined,
            })
          }
        />
      </div>

      <div className="flex flex-col gap-1">
        <div className="flex items-center text-xs text-text-secondary">
          Categories
        </div>
        <CategoryPicker
          value={filters.category_ids ?? []}
          label=""
          onChange={(category_ids) =>
            onChange({
              ...filters,
              category_ids: category_ids.length > 0 ? category_ids : undefined,
            })
          }
        />
      </div>

      {!hideTxnType && (
        <div className="flex flex-col gap-1">
          <TxnTypeCheckboxRow
            value={filters.txn_type}
            allowTransfer={allowTransfer}
            onChange={(txn_type) => onChange({ ...filters, txn_type })}
          />
        </div>
      )}

      {/* Settled/Pending is a transactions-only filter (the only source
          publishing a ``status`` field), so the control is offered only
          for transactions widgets — mirroring the Transfer type gate.
          Status now cascades from the canvas, so it carries the same
          "Overrides canvas" pill as the date range when the widget value
          differs from the inherited canvas status. */}
      {allowTransfer && (
        <div className="flex flex-col gap-1">
          <div className="flex items-center text-xs text-text-secondary">
            Status
            {isFieldOverridden("status", filters, canvasFilters) && (
              <OverridePill />
            )}
          </div>
          <StatusFilter
            value={filters.status}
            label=""
            ariaPrefix="Widget status"
            onChange={(status) => onChange({ ...filters, status })}
          />
        </div>
      )}

      {/* Amount is a transactions-only filter (the only source
          publishing an ``amount`` field), so the control is offered only
          for transactions widgets — mirroring the Status gate. */}
      {allowTransfer && (
        <div className="flex flex-col gap-1">
          <AmountRangeFilter
            value={filters.amount_range}
            ariaPrefix="Widget amount"
            onChange={(amount_range) =>
              onChange({ ...filters, amount_range })
            }
          />
        </div>
      )}

      {/* "Include transfers & adjustments" is transactions-only. By default
          reports exclude transfer legs, manual balance adjustments, and
          reverted (skipped/rejected) reconciliation rows — matching Budgets,
          Forecast, and the Sankey. This opt-in re-includes transfer legs +
          manual adjustments; reverted rows stay excluded server-side. */}
      {allowTransfer && (
        <label className="flex items-start gap-2 text-xs text-text-secondary">
          <input
            type="checkbox"
            className="mt-0.5"
            aria-label="Include transfers and adjustments"
            aria-describedby="include-non-reportable-help"
            checked={!!filters.include_non_reportable}
            onChange={(e) =>
              onChange({
                ...filters,
                include_non_reportable: e.target.checked || undefined,
              })
            }
          />
          <span className="flex flex-col gap-0.5">
            <span>Include transfers &amp; adjustments</span>
            <span
              id="include-non-reportable-help"
              className="text-[11px] text-text-muted"
            >
              By default reports leave out transfers and balance adjustments,
              like Budgets and Forecast do. Turn this on to count them.
            </span>
          </span>
        </label>
      )}

      <TagFilter
        value={filters.tag_names ?? []}
        match={(filters.tag_match ?? "all") as TagMatch}
        onChange={({ tag_names, tag_match }) =>
          onChange({
            ...filters,
            tag_names: tag_names.length > 0 ? tag_names : undefined,
            tag_match: tag_names.length > 0 ? tag_match : undefined,
          })
        }
      />
    </div>
  );
}

function TxnTypeCheckboxRow({
  value,
  allowTransfer,
  onChange,
}: {
  value: TxnType[] | undefined;
  allowTransfer: boolean;
  onChange: (next: TxnType[] | undefined) => void;
}) {
  // ``asTxnTypeArray`` also coerces a legacy single-string value (old
  // saved reports) into an array, so the control renders correctly for
  // both shapes. No "Any" choice — zero checked boxes IS "Any".
  const selected = asTxnTypeArray(value) ?? [];
  const choices: Array<{ value: TxnType; label: string }> = [
    { value: "income", label: "Income" },
    { value: "expense", label: "Expense" },
    // ``transfer`` is a transactions-only concept; omit it for sources
    // whose ``type`` can't be a transfer (recurring / accounts).
    ...(allowTransfer
      ? ([{ value: "transfer", label: "Transfer" }] as const)
      : []),
  ];
  // Self-heal: if a persisted ``transfer`` survives on a non-transactions
  // source (where the Transfer box is hidden), strip it once so the widget
  // never queries a type the source 422s. Depending on the boolean keeps
  // the effect from re-firing after the value settles (cleaned value no
  // longer contains ``transfer`` → condition false → no loop).
  const hasIllegalTransfer = !allowTransfer && selected.includes("transfer");
  useEffect(() => {
    if (hasIllegalTransfer) {
      const cleaned = selected.filter((t) => t !== "transfer");
      onChange(cleaned.length > 0 ? cleaned : undefined);
    }
    // Depend ONLY on the boolean: it flips true at most once (the
    // onChange clears transfer → selected loses it → false), so the
    // effect fires exactly when needed and never re-runs on unrelated
    // parent re-renders (``onChange`` is a fresh ref each render).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasIllegalTransfer]);

  function toggle(t: TxnType) {
    const next = selected.includes(t)
      ? selected.filter((x) => x !== t)
      : [...selected, t];
    onChange(next.length > 0 ? next : undefined);
  }

  return (
    <>
      <div className="text-xs text-text-secondary">Transaction type</div>
      <div className="flex flex-wrap gap-3 text-xs text-text-secondary">
        {choices.map((c) => (
          <label key={c.value} className="flex items-center gap-1">
            <input
              type="checkbox"
              aria-label={`Widget transaction type ${c.label}`}
              checked={selected.includes(c.value)}
              onChange={() => toggle(c.value)}
            />
            <span>{c.label}</span>
          </label>
        ))}
      </div>
    </>
  );
}
