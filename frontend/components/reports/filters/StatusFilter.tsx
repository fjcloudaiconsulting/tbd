"use client";

/**
 * Settled/Pending status filter — a 3-way radio row (All / Settled /
 * Pending). "All" emits no filter (``undefined``); Settled/Pending
 * emit ``{ field: "status", op: "eq", value }`` via ``resolveFilters``.
 *
 * Transactions-only: the ``status`` filter is published only by the
 * transactions source, so ``FilterEditor`` renders this control only
 * for transactions widgets (mirroring how the Transfer type is gated).
 *
 * Accessible: the three options live in a ``<fieldset>`` with a
 * ``<legend>`` so assistive tech announces them as one labelled group.
 */
import { useId } from "react";

import type { TxnStatus } from "@/lib/reports/types";

interface Props {
  value: TxnStatus | undefined;
  onChange: (next: TxnStatus | undefined) => void;
  /** Legend label shown above the radio row. */
  label?: string;
  /** Aria-prefix on each radio, e.g. "Widget" → "Widget status Pending". */
  ariaPrefix?: string;
}

// ``undefined`` models the "All" choice — no status filter emitted.
const CHOICES: Array<{ value: TxnStatus | undefined; label: string }> = [
  { value: undefined, label: "All" },
  { value: "settled", label: "Settled" },
  { value: "pending", label: "Pending" },
];

export default function StatusFilter({
  value,
  onChange,
  label = "Status",
  ariaPrefix = "Status",
}: Props) {
  const radioName = useId();
  return (
    <fieldset className="flex flex-col gap-1.5" data-testid="status-filter">
      <legend className="text-xs text-text-secondary">{label}</legend>
      <div className="flex flex-wrap gap-3 text-xs text-text-secondary">
        {CHOICES.map((c) => (
          <label key={c.label} className="flex items-center gap-1">
            <input
              type="radio"
              name={radioName}
              data-testid={`status-filter-${c.value ?? "all"}`}
              aria-label={`${ariaPrefix} ${c.label}`}
              checked={(value ?? undefined) === c.value}
              onChange={() => onChange(c.value)}
            />
            <span>{c.label}</span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}
