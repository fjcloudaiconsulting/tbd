"use client";

import type { ReactNode } from "react";

import HelpTooltip from "@/components/help/HelpTooltip";
import { input, label as labelCls } from "@/lib/styles";

/**
 * The "Repeats" fields: frequency, how many occurrences, auto-settle (TBD-762).
 *
 * Shared by the quick-entry form (`floating/TransactionForm`, `variant`
 * "inline") and the recurring page's Edit series dialog ("stacked").
 *
 * - "inline" renders the fields as siblings for the caller's wrap row: hidden
 *   labels, and a blank count means "repeat indefinitely".
 * - "stacked" gives every field a visible label and makes the end explicit:
 *   Never, or After N occurrences. An existing series can be turned back to
 *   open-ended, which a blank box would not say clearly.
 *
 * The count is a TOTAL including what the series has already delivered
 * (TBD-275), in both variants.
 */

export const FREQUENCY_OPTIONS = [
  { value: "weekly", label: "Weekly" },
  { value: "biweekly", label: "Every 2 weeks" },
  { value: "monthly", label: "Monthly" },
  { value: "quarterly", label: "Quarterly" },
  { value: "yearly", label: "Yearly" },
] as const;

interface CommonProps {
  /** Prefix for element ids: `${idPrefix}-freq`, `${idPrefix}-occurrences`. */
  idPrefix: string;
  frequency: string;
  onFrequencyChange: (value: string) => void;
  occurrenceCount: string;
  onOccurrenceCountChange: (value: string) => void;
  autoSettle: boolean;
  onAutoSettleChange: (value: boolean) => void;
}

interface InlineProps extends CommonProps {
  variant?: "inline";
}

interface StackedProps extends CommonProps {
  variant: "stacked";
  endsNever: boolean;
  onEndsNeverChange: (never: boolean) => void;
  /** Shown under the Ends choice, e.g. "3 of 12 done". */
  progress?: ReactNode;
  /** Error for the count, shown under it and wired to aria-describedby. */
  countError?: string | null;
}

export type RecurrenceFieldsProps = InlineProps | StackedProps;

function FrequencySelect({
  id,
  value,
  onChange,
}: {
  id: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <select id={id} value={value} onChange={(e) => onChange(e.target.value)} className={input}>
      {FREQUENCY_OPTIONS.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

export default function RecurrenceFields(props: RecurrenceFieldsProps) {
  const { idPrefix } = props;
  const freqId = `${idPrefix}-freq`;
  const countId = `${idPrefix}-occurrences`;

  if (props.variant === "stacked") {
    const { endsNever, onEndsNeverChange, progress, countError } = props;
    const describedBy =
      [progress ? `${countId}-progress` : null, countError ? `${countId}-error` : null]
        .filter(Boolean)
        .join(" ") || undefined;
    return (
      <>
        <div>
          <label htmlFor={freqId} className={labelCls}>
            Frequency
          </label>
          <div className="flex items-center gap-1">
            <FrequencySelect id={freqId} value={props.frequency} onChange={props.onFrequencyChange} />
            <HelpTooltip k="tx.frequency" />
          </div>
        </div>
        <fieldset>
          <legend className={labelCls}>Ends</legend>
          <div className="flex flex-col gap-2">
            <label className="flex min-h-[44px] items-center gap-2 text-sm text-text-secondary md:min-h-0">
              <input
                type="radio"
                name={`${idPrefix}-ends`}
                checked={endsNever}
                onChange={() => onEndsNeverChange(true)}
              />
              Never
            </label>
            <div className="flex flex-wrap items-center gap-2">
              <label className="flex min-h-[44px] items-center gap-2 text-sm text-text-secondary md:min-h-0">
                <input
                  type="radio"
                  name={`${idPrefix}-ends`}
                  checked={!endsNever}
                  onChange={() => onEndsNeverChange(false)}
                />
                After
              </label>
              <label htmlFor={countId} className="sr-only">
                Number of occurrences
              </label>
              <input
                id={countId}
                // type="text": a number input coerces unparseable text to "",
                // which would read as "never ends" with no feedback.
                type="text"
                inputMode="numeric"
                disabled={endsNever}
                value={props.occurrenceCount}
                onChange={(e) => props.onOccurrenceCountChange(e.target.value)}
                aria-invalid={countError ? true : undefined}
                aria-describedby={describedBy}
                className={`!w-24 ${input}`}
              />
              <span className="text-sm text-text-secondary">occurrences</span>
              <HelpTooltip k="tx.occurrence-count" />
            </div>
          </div>
          {progress && (
            <p id={`${countId}-progress`} className="mt-1 text-xs text-text-muted">
              {progress}
            </p>
          )}
          {countError && (
            <p id={`${countId}-error`} className="mt-1 text-xs text-danger">
              {countError}
            </p>
          )}
        </fieldset>
        <label className="flex min-h-[44px] items-center gap-2 text-sm text-text-secondary md:min-h-0">
          <input
            type="checkbox"
            checked={props.autoSettle}
            onChange={(e) => props.onAutoSettleChange(e.target.checked)}
            className="rounded border-border"
          />
          Auto-settle
          <HelpTooltip k="tx.auto-settle" />
        </label>
      </>
    );
  }

  return (
    <>
      <div className="flex items-center gap-1">
        <label htmlFor={freqId} className="sr-only">
          Frequency
        </label>
        <FrequencySelect id={freqId} value={props.frequency} onChange={props.onFrequencyChange} />
        <HelpTooltip k="tx.frequency" />
      </div>
      {/* TBD-275. Account-agnostic by construction: this block renders
          for every account type, so "available on checking and credit
          card accounts" is satisfied without a type branch — and adding
          one would hide instalment plans on the savings/cash accounts
          people also put them on. Blank is the common case, so the
          placeholder carries the meaning rather than a helper line
          (quiet-by-default, docs/product/PRODUCT.md). */}
      <div className="flex items-center gap-1">
        <label htmlFor={countId} className="sr-only">
          Number of payments
        </label>
        <input
          id={countId}
          // ⚠ type="text", NOT type="number". A number input silently
          // COERCES unparseable text to the empty string, so "abc" would
          // read as blank and quietly turn a 4-payment plan into an
          // open-ended one with no feedback at all. It also moves
          // rejection into the browser's native constraint bubble, which
          // means two half-overlapping enforcement points instead of the
          // one guard in the caller's submit that every other field uses.
          type="text"
          inputMode="numeric"
          placeholder="Payments (optional)"
          aria-describedby={`${countId}-hint`}
          value={props.occurrenceCount}
          onChange={(e) => props.onOccurrenceCountChange(e.target.value)}
          className={`!w-44 ${input}`}
        />
        <HelpTooltip k="tx.occurrence-count" />
      </div>
      <p id={`${countId}-hint`} className="w-full text-[10px] text-text-muted">
        Leave blank to repeat indefinitely. Counting this one, so a
        4-payment plan stops after three more.
      </p>
      <label className="flex items-center gap-2 text-xs text-text-muted">
        <input
          type="checkbox"
          checked={props.autoSettle}
          onChange={(e) => props.onAutoSettleChange(e.target.checked)}
          className="rounded border-border"
        />
        Auto-settle
        <HelpTooltip k="tx.auto-settle" />
      </label>
    </>
  );
}
