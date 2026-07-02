"use client";

/**
 * Amount range filter — two numeric inputs (Min / Max) reading and
 * writing ``amount_range: { min?, max? }``. An empty input clears its
 * bound (``undefined``); ``resolveFilters`` then emits a ``gte`` clause
 * for ``min``, an ``lte`` clause for ``max`` (or a ``between`` when both
 * are set), and nothing when both are undefined.
 *
 * Transactions-only: ``amount`` is published only by the transactions
 * source, so ``FilterEditor`` renders this control only for transactions
 * widgets (mirroring how Status and the Transfer type are gated).
 *
 * Accessible: the two inputs live in a ``<fieldset>`` with a
 * ``<legend>`` so assistive tech announces them as one labelled group,
 * each input carrying its own ``aria-label``.
 */
import { input as inputClass } from "@/lib/styles";

interface AmountRange {
  min?: number;
  max?: number;
}

interface Props {
  value: AmountRange | undefined;
  onChange: (next: AmountRange | undefined) => void;
  /** Legend label shown above the input row. */
  label?: string;
  /** Aria-prefix on each input, e.g. "Widget" → "Widget amount min". */
  ariaPrefix?: string;
}

// Empty string → ``undefined`` (no bound); never emit NaN.
function parseBound(raw: string): number | undefined {
  if (raw.trim() === "") return undefined;
  const n = Number(raw);
  return Number.isNaN(n) ? undefined : n;
}

// Collapse an all-``undefined`` range back to ``undefined`` so we never
// persist an empty ``{}`` amount_range blob.
function normalize(range: AmountRange): AmountRange | undefined {
  if (range.min === undefined && range.max === undefined) return undefined;
  return range;
}

export default function AmountRangeFilter({
  value,
  onChange,
  label = "Amount range",
  ariaPrefix = "Amount",
}: Props) {
  return (
    <fieldset className="flex flex-col gap-1" data-testid="amount-range-filter">
      <legend className="text-xs text-text-secondary">{label}</legend>
      <div className="flex gap-2">
        <input
          type="number"
          step="0.01"
          inputMode="decimal"
          aria-label={`${ariaPrefix} min`}
          placeholder="min"
          value={value?.min ?? ""}
          onChange={(e) =>
            onChange(
              normalize({
                ...(value ?? {}),
                min: parseBound(e.target.value),
              }),
            )
          }
          className={inputClass}
        />
        <input
          type="number"
          step="0.01"
          inputMode="decimal"
          aria-label={`${ariaPrefix} max`}
          placeholder="max"
          value={value?.max ?? ""}
          onChange={(e) =>
            onChange(
              normalize({
                ...(value ?? {}),
                max: parseBound(e.target.value),
              }),
            )
          }
          className={inputClass}
        />
      </div>
    </fieldset>
  );
}
