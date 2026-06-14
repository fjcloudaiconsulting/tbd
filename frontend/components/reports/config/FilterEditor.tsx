"use client";

/**
 * Per-widget filter editor (date / accounts / categories / txn_type /
 * amount_range / tags).
 *
 * Phase 4b: ``date_range`` is the ONLY canvas-shared field, so it is the
 * only field that can carry the "Overrides canvas" pill — the pill fires
 * when the widget date DIFFERS from the canvas date, via
 * ``isFieldOverridden`` from ``lib/reports/resolve`` (not reimplemented).
 * Accounts, categories, txn_type, amount_range and tags are all
 * widget-only now (the canvas can't hold them), so they NEVER show the
 * override pill — they're plain per-widget controls.
 */
import { useEffect, useId } from "react";

import AccountFilter from "@/components/reports/filters/AccountFilter";
import CategoryPicker from "@/components/reports/filters/CategoryPicker";
import DatePresetChips from "@/components/reports/filters/DatePresetChips";
import TagFilter from "@/components/reports/filters/TagFilter";
import { isFieldOverridden } from "@/lib/reports/resolve";
import type {
  CanvasFilters,
  Dataset,
  TagMatch,
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

      <div className="flex flex-col gap-1">
        <TxnTypeRadioRow
          value={filters.txn_type}
          allowTransfer={allowTransfer}
          onChange={(txn_type) => onChange({ ...filters, txn_type })}
        />
      </div>

      <div className="flex flex-col gap-1">
        <div className="text-xs text-text-secondary">Amount range</div>
        <div className="flex gap-2">
          <input
            type="number"
            aria-label="Widget amount min"
            placeholder="min"
            value={filters.amount_range?.min ?? ""}
            onChange={(e) =>
              onChange({
                ...filters,
                amount_range: {
                  ...(filters.amount_range ?? {}),
                  min: e.target.value === "" ? undefined : Number(e.target.value),
                },
              })
            }
            className="w-full rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
          />
          <input
            type="number"
            aria-label="Widget amount max"
            placeholder="max"
            value={filters.amount_range?.max ?? ""}
            onChange={(e) =>
              onChange({
                ...filters,
                amount_range: {
                  ...(filters.amount_range ?? {}),
                  max: e.target.value === "" ? undefined : Number(e.target.value),
                },
              })
            }
            className="w-full rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
          />
        </div>
      </div>

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

function TxnTypeRadioRow({
  value,
  allowTransfer,
  onChange,
}: {
  value: "income" | "expense" | "transfer" | undefined;
  allowTransfer: boolean;
  onChange: (next: "income" | "expense" | "transfer" | undefined) => void;
}) {
  const name = useId();
  const choices: Array<{ value: "" | "income" | "expense" | "transfer"; label: string }> = [
    { value: "", label: "Any" },
    { value: "income", label: "Income" },
    { value: "expense", label: "Expense" },
    // ``transfer`` is a transactions-only concept; omit it for sources
    // whose ``type`` can't be a transfer (recurring / accounts).
    ...(allowTransfer
      ? ([{ value: "transfer", label: "Transfer" }] as const)
      : []),
  ];
  // Self-heal: when a persisted ``txn_type`` value isn't among the rendered
  // choices (e.g. ``transfer`` survives in a saved config but the widget is
  // now on a non-transactions source where Transfer is hidden), reset it to
  // "Any" once so the control never shows a phantom no-selection-with-stale-
  // value state. ``onChange(undefined)`` makes ``value`` undefined, which IS
  // in-range, so the effect won't re-fire (no loop). The transactions case
  // keeps ``transfer`` because it's a rendered choice there.
  const valueInRange =
    value === undefined || choices.some((c) => c.value === value);
  useEffect(() => {
    if (!valueInRange) onChange(undefined);
  }, [valueInRange, onChange]);
  return (
    <>
      <div className="text-xs text-text-secondary">Transaction type</div>
      <div className="flex flex-wrap gap-3 text-xs text-text-secondary">
        {choices.map((c) => (
          <label key={c.value} className="flex items-center gap-1">
            <input
              type="radio"
              name={name}
              aria-label={`Widget transaction type ${c.label}`}
              checked={(value ?? "") === c.value}
              onChange={() => onChange(c.value === "" ? undefined : (c.value as "income" | "expense" | "transfer"))}
            />
            <span>{c.label}</span>
          </label>
        ))}
      </div>
    </>
  );
}
