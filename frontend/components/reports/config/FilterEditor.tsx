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
import { useId } from "react";

import AccountFilter from "@/components/reports/filters/AccountFilter";
import AmountRangeFilter from "@/components/reports/filters/AmountRangeFilter";
import CategoryPicker from "@/components/reports/filters/CategoryPicker";
import DatePresetChips from "@/components/reports/filters/DatePresetChips";
import StatusFilter from "@/components/reports/filters/StatusFilter";
import TagFilter from "@/components/reports/filters/TagFilter";
import { publishedFilterKeys } from "@/lib/reports/resolve";
import { useReportSources } from "@/lib/reports/use-report-sources";
import {
  asTxnTypeArray,
  isFieldOverridden,
  setTransferMode,
  transferMode,
  type TransferMode,
} from "@/lib/reports/resolve";
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
  hideTypeControls = false,
  onChange,
}: {
  filters: WidgetFilters;
  canvasFilters: CanvasFilters;
  dataset: Dataset;
  /**
   * When true, hides the transaction-type checkboxes AND the transfers
   * radio group entirely. Used for widget types where both are a backend
   * no-op (Sankey: ``txn_type`` is ignored, and neither
   * ``include_non_reportable`` nor a ``transfer`` filter is accepted by the
   * ``extra="forbid"`` Sankey endpoint — TBD-471), so the user is never
   * shown a control that has no effect on the chart. Named for BOTH
   * controls now (was ``hideTxnType``, txn_type-only) — one flag, no new
   * prop.
   */
  hideTypeControls?: boolean;
  onChange: (next: WidgetFilters) => void;
}) {
  // TBD-381: SUBTRACTIVE. A control is offered iff the selected source
  // publishes its field. Before this the set was fixed and transactions-shaped,
  // narrowed only by `allowTransfer`, so it lied in BOTH directions:
  //
  //   * offered `category_id` on net worth -> silently dropped by the
  //     shared-canvas contract, the "does nothing" the owner reported;
  //   * offered `txn_type` / `tag_name` on net worth / accounts /
  //     credit_utilization -> those are NOT shared-canvas fields, so
  //     `validate_against_catalog` RAISES and the widget renders
  //     "Couldn't load" with no explanation;
  //   * HID `amount` on recurring, which publishes it.
  //
  // ⚠ Unknown catalog means ALLOW (see `sourceSupportsField`) so a cold cache
  // cannot silently strip every control.
  const { sources } = useReportSources();
  const published = publishedFilterKeys(sources, dataset);
  const has = (key: keyof WidgetFilters) => published.has(key);
  const transferGroupId = useId();
  return (
    <div
      data-testid="filter-editor-root"
      className="flex flex-col gap-4 rounded-md border border-border bg-bg p-3"
    >
      <div className="text-[11px] font-medium uppercase tracking-wider text-text-muted">
        Filters (this widget)
      </div>

      {has("date_range") && (
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
      )}

      {has("account_ids") && (
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
      )}

      {has("category_ids") && (
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
      )}

      {has("txn_type") && !hideTypeControls && (
        <div className="flex flex-col gap-1">
          <TxnTypeCheckboxRow
            value={filters.txn_type}
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
      {has("status") && (
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

      {/* ⚠ Amount is NOT transactions-only, contrary to what this comment
          used to say. `recurring` publishes an ``amount`` filter too (kind
          "number" rather than "amount" -- the same concept under two kinds,
          which is why `kind` is not a sound dispatch key). The old
          `dataset === "transactions"` gate HID a control recurring supports.
          Catalog-gated now, so both get it and neither is guessed at. */}
      {has("amount_range") && (
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

      {/* TBD-471 RULING 1: one 3-state radio axis replaces the old
          "Include transfers & adjustments" checkbox. Gated on the CATALOG
          (``has("transfers_only")`` -> does the source publish a
          ``transfer`` filter field), never on a
          ``dataset === "transactions"`` hand-gate — that hand-gate is
          exactly what ``filter-editor-catalog.test.tsx`` exists to kill.
          ``hideTypeControls`` additionally hides it on Sankey, whose
          ``extra="forbid"`` endpoint accepts neither key. */}
      {has("transfers_only") && !hideTypeControls && (
        <fieldset
          className="flex flex-col gap-1.5"
          aria-describedby={`${transferGroupId}-help`}
        >
          <legend className="text-xs text-text-secondary">Transfers</legend>
          <div className="flex flex-col">
            {(
              [
                { value: "exclude", label: "Exclude transfers (default)" },
                { value: "include", label: "Include transfers & adjustments" },
                { value: "only", label: "Only transfers" },
              ] satisfies Array<{ value: TransferMode; label: string }>
            ).map((c) => (
              <label
                key={c.value}
                className="flex min-h-[24px] items-center gap-2 text-xs text-text-secondary"
              >
                <input
                  type="radio"
                  name={transferGroupId}
                  checked={transferMode(filters) === c.value}
                  onChange={() => onChange(setTransferMode(filters, c.value))}
                />
                <span>{c.label}</span>
              </label>
            ))}
          </div>
          <p id={`${transferGroupId}-help`} className="text-[11px] text-text-muted">
            Shows both sides of every transfer, so totals and counts count
            each one twice. Break down by Account to see the money move.
          </p>
        </fieldset>
      )}

      {/* ⚠ Gated like every other control. This one was MISSED in the first
          pass, and the miss shipped half the reported bug: `tag_name` is
          published only by transactions and is NOT a shared-canvas field, so
          picking a tag on a net-worth widget makes validate_against_catalog
          RAISE -- the widget renders "Couldn't load" with no explanation, and
          the only escape is to reopen the popover and unpick it. */}
      {has("tag_names") && (
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
      )}
    </div>
  );
}

function TxnTypeCheckboxRow({
  value,
  onChange,
}: {
  value: TxnType[] | undefined;
  onChange: (next: TxnType[] | undefined) => void;
}) {
  // ``asTxnTypeArray`` also coerces a legacy single-string value (old
  // saved reports) into an array, so the control renders correctly for
  // both shapes. No "Any" choice — zero checked boxes IS "Any".
  //
  // TBD-471: ``transfer`` retired from ``TxnType`` entirely (it is now its
  // own filter axis — the radio group above), so there is no longer a
  // conditional third choice and no self-heal effect: ``asTxnTypeArray``
  // itself is the chokepoint that drops a persisted ``"transfer"`` on
  // read, for every dataset, the same way it always dropped any other
  // unknown value.
  const selected = asTxnTypeArray(value) ?? [];
  const choices: Array<{ value: TxnType; label: string }> = [
    { value: "income", label: "Income" },
    { value: "expense", label: "Expense" },
  ];

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
