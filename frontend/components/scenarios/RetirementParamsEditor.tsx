"use client";

/**
 * Retirement params editor (PR2 of the Plans train).
 *
 * Shape mirrors the backend `RetirementParams` Pydantic model.
 * The `contribution_curve` editor is an inline table where the
 * user can add / remove (date, monthly) rows. The component
 * enforces strictly-ascending date order client-side to match
 * the server-side validator; out-of-order entries surface a
 * validation message and disable the persist debounce in the
 * parent (which checks via the `onValidityChange` callback).
 */

import { useEffect, useMemo, useState } from "react";

import {
  btnSecondary,
  error as errorCls,
  input,
  label as labelCls,
} from "@/lib/styles";

export interface RetirementCurveStep {
  from: string; // YYYY-MM-DD
  monthly: string; // decimal string
}

export interface RetirementParamsShape {
  scenario_type: "retirement";
  target_retirement_date: string;
  currency: string;
  monthly_contribution: string;
  contribution_account_id: number;
  target_balance: string;
  annual_return_pct: string;
  inflation_pct: string;
  contribution_curve: RetirementCurveStep[];
}

export interface AccountChoice {
  id: number;
  name: string;
  currency: string;
}

function validateCurve(curve: RetirementCurveStep[]): string | null {
  let prev: string | null = null;
  for (const step of curve) {
    if (!step.from) return "Each curve row needs a 'from' date.";
    if (prev !== null && step.from <= prev) {
      return "Curve rows must be strictly ascending by 'from' date.";
    }
    prev = step.from;
  }
  return null;
}

// In-context microcopy lives next to the field it explains so the
// "lazy user" path the product owner flagged (someone who opens the
// form should not have to leave the page to learn what each input
// means) stays satisfied without bloating component logic.
const HELP_TARGET_DATE = "When you plan to stop earning income.";
const HELP_TARGET_BALANCE =
  "What you want to have invested at retirement, in today's money. The chart's red dashed line plots this in real (inflation-adjusted) terms.";
const HELP_MONTHLY =
  "Default amount you set aside each month. Override for specific date ranges using the curve below.";
const HELP_RETURN =
  "Expected annual investment return after fees. Long-term stock market average is around 6 to 8 percent.";
const HELP_INFLATION =
  "How fast prices rise per year. Eurozone target is 2 percent; historical 30-year average is around 2.5 to 3 percent.";
const HELP_ACCOUNT =
  "Which account receives the monthly contribution in the projection.";
const HELP_CURVE =
  "Optional. Each row sets a new monthly contribution starting on a given date. Useful when you expect to save more later (for example, after kids leave school). Rows must be in chronological order. The base contribution applies before the first row's date.";

const helpText = "mt-1 max-w-prose text-xs text-text-muted";

export function RetirementParamsEditor({
  params,
  setParams,
  accounts,
  onValidityChange,
}: {
  params: Record<string, unknown>;
  setParams: (next: Record<string, unknown>) => void;
  accounts: AccountChoice[];
  onValidityChange?: (valid: boolean) => void;
}) {
  function set<K extends keyof RetirementParamsShape>(
    key: K,
    value: RetirementParamsShape[K],
  ) {
    setParams({ ...params, [key]: value });
  }

  const curve: RetirementCurveStep[] = useMemo(() => {
    const raw = params.contribution_curve;
    if (Array.isArray(raw)) {
      return raw.map((step) => {
        const s = step as Record<string, unknown>;
        return {
          from: String(s.from ?? ""),
          monthly: String(s.monthly ?? "0"),
        };
      });
    }
    return [];
  }, [params.contribution_curve]);

  const [curveError, setCurveError] = useState<string | null>(null);

  useEffect(() => {
    const err = validateCurve(curve);
    setCurveError(err);
    onValidityChange?.(err === null);
  }, [curve, onValidityChange]);

  function updateCurve(next: RetirementCurveStep[]) {
    set("contribution_curve", next);
  }

  function addRow() {
    updateCurve([...curve, { from: "", monthly: "0" }]);
  }

  function removeRow(index: number) {
    updateCurve(curve.filter((_, i) => i !== index));
  }

  function patchRow(index: number, patch: Partial<RetirementCurveStep>) {
    updateCurve(curve.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  }

  return (
    <>
      <div>
        <label className={labelCls} htmlFor="ret-target-date">Target retirement date</label>
        <input
          id="ret-target-date"
          type="date"
          value={(params.target_retirement_date as string) ?? ""}
          onChange={(e) => set("target_retirement_date", e.target.value)}
          className={input}
          data-testid="ret-target-date"
          aria-describedby="ret-target-date-hint"
        />
        <p id="ret-target-date-hint" className={helpText}>{HELP_TARGET_DATE}</p>
      </div>
      <div>
        <label className={labelCls} htmlFor="ret-target-balance">Target balance</label>
        <input
          id="ret-target-balance"
          type="number"
          step="0.01"
          min="0"
          value={(params.target_balance as string) ?? "0"}
          onChange={(e) => set("target_balance", e.target.value)}
          className={input}
          data-testid="ret-target-balance"
          aria-describedby="ret-target-balance-hint"
        />
        <p id="ret-target-balance-hint" className={helpText}>{HELP_TARGET_BALANCE}</p>
      </div>
      <div>
        <label className={labelCls} htmlFor="ret-monthly">Monthly contribution (base)</label>
        <input
          id="ret-monthly"
          type="number"
          step="0.01"
          min="0"
          value={(params.monthly_contribution as string) ?? "0"}
          onChange={(e) => set("monthly_contribution", e.target.value)}
          className={input}
          data-testid="ret-monthly"
          aria-describedby="ret-monthly-hint"
        />
        <p id="ret-monthly-hint" className={helpText}>{HELP_MONTHLY}</p>
      </div>
      <div>
        <label className={labelCls} htmlFor="ret-return">Annual return percent</label>
        <input
          id="ret-return"
          type="number"
          step="0.01"
          min="0"
          max="100"
          value={(params.annual_return_pct as string) ?? "6.0"}
          onChange={(e) => set("annual_return_pct", e.target.value)}
          className={input}
          data-testid="ret-return"
          aria-describedby="ret-return-hint"
        />
        <p id="ret-return-hint" className={helpText}>{HELP_RETURN}</p>
      </div>
      <div>
        <label className={labelCls} htmlFor="ret-inflation">Annual inflation percent</label>
        <input
          id="ret-inflation"
          type="number"
          step="0.01"
          min="0"
          max="100"
          value={(params.inflation_pct as string) ?? "2.5"}
          onChange={(e) => set("inflation_pct", e.target.value)}
          className={input}
          data-testid="ret-inflation"
          aria-describedby="ret-inflation-hint"
        />
        <p id="ret-inflation-hint" className={helpText}>{HELP_INFLATION}</p>
      </div>
      <div>
        <label className={labelCls} htmlFor="ret-account">Contribution account</label>
        <select
          id="ret-account"
          value={(params.contribution_account_id as number) ?? ""}
          onChange={(e) => set("contribution_account_id", Number(e.target.value))}
          className={input}
          data-testid="ret-account"
          aria-describedby="ret-account-hint"
        >
          {accounts.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name} ({a.currency})
            </option>
          ))}
        </select>
        <p id="ret-account-hint" className={helpText}>{HELP_ACCOUNT}</p>
      </div>
      <div>
        <p className={`${labelCls} mb-2`}>Contribution curve (step function)</p>
        <p className={`${helpText} mt-0 mb-2`}>{HELP_CURVE}</p>
        {curveError && (
          <p className={`mb-2 text-xs ${errorCls}`} data-testid="ret-curve-error">
            {curveError}
          </p>
        )}
        <table className="mb-2 w-full text-xs" data-testid="ret-curve-table">
          <thead>
            <tr className="text-left text-text-muted">
              <th className="pb-1 font-normal">From</th>
              <th className="pb-1 font-normal">Monthly</th>
              <th className="pb-1" />
            </tr>
          </thead>
          <tbody>
            {curve.length === 0 && (
              <tr>
                <td colSpan={3} className="py-2 text-text-muted">
                  No steps yet.
                </td>
              </tr>
            )}
            {curve.map((row, i) => (
              <tr key={i} data-testid={`ret-curve-row-${i}`}>
                <td className="pr-2 align-top">
                  <input
                    type="date"
                    className={input}
                    value={row.from}
                    onChange={(e) => patchRow(i, { from: e.target.value })}
                    data-testid={`ret-curve-from-${i}`}
                  />
                </td>
                <td className="pr-2 align-top">
                  <input
                    type="number"
                    step="0.01"
                    min="0"
                    className={input}
                    value={row.monthly}
                    onChange={(e) => patchRow(i, { monthly: e.target.value })}
                    data-testid={`ret-curve-monthly-${i}`}
                  />
                </td>
                <td className="pt-1 align-top">
                  <button
                    type="button"
                    className="text-xs text-danger underline-offset-2 hover:underline"
                    onClick={() => removeRow(i)}
                    data-testid={`ret-curve-remove-${i}`}
                  >
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <button
          type="button"
          className={`${btnSecondary} sm:min-h-0`}
          onClick={addRow}
          data-testid="ret-curve-add"
        >
          + Add step
        </button>
      </div>
    </>
  );
}
