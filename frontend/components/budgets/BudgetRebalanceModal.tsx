"use client";

// BudgetRebalanceModal — zero-sum free allocation (TBD-461).
//
// Free allocation, not a diff review: every budget gets its own slider +
// text control, any direction, and Apply is enabled only once the net
// change is exactly zero. "Use suggestions" (AI, entitlement-gated) is a
// preset that fills the text controls; it never fetches on open and never
// auto-applies. One POST on Apply, atomic on the server.

import { useEffect, useMemo, useRef, useState } from "react";

import { apiFetch, ApiResponseError, extractErrorMessage } from "@/lib/api";
import { btnPrimary, btnSecondary, card, error as errorCls, input as inputCls } from "@/lib/styles";
import { maskMoneyText } from "@/lib/format";
import { useBalancesHidden, useMoney } from "@/lib/hooks/use-org-currency";
import { useFocusTrap } from "@/lib/hooks/use-focus-trap";

export type RebalanceStatus =
  | "ok"
  | "empty_no_budgets"
  | "empty_no_history"
  | "empty_no_surplus"
  | "llm_unavailable";

export interface RebalanceSuggestion {
  category_id: number;
  category_name: string;
  current_amount: string | number;
  suggested_amount: string | number;
  delta_amount: string | number;
  reasoning: string;
}

export interface RebalanceResponse {
  status: RebalanceStatus;
  period_start: string | null;
  suggestions: RebalanceSuggestion[];
  summary: string;
}

interface Budget {
  id: number;
  category_id: number;
  category_name: string;
  amount: string | number;
}

interface Row {
  id: number;
  category_id: number;
  category_name: string;
  baseCents: number;
  baseText: string;
}

interface Props {
  open: boolean;
  budgets: Budget[];
  /** Gate for the "Use suggestions" preset (current period + AI entitled/configured). */
  canSuggest: boolean;
  /** Called after Apply succeeds, or before re-snapshotting after a 409/404. */
  onApplied: () => Promise<void>;
  onClose: () => void;
}

const ERROR_INVALID = "Use a number with up to 2 decimals";
const RECONCILE_MESSAGE =
  "Budgets changed since you opened this. Reloaded the latest amounts.";
const RECONCILE_FAILED_MESSAGE =
  "Could not reload the latest amounts. Close and reopen.";

function toNumber(value: string | number): number {
  return typeof value === "string" ? Number(value) : value;
}

function centsToText(cents: number): string {
  return (cents / 100).toFixed(2);
}

const MONEY_RE = /^(\d+(\.\d{0,2})?|\.\d{1,2})$/;

/** Parses a money string into integer cents, or null if invalid. Splits on
 *  "." rather than multiplying by 100, so "12." -> 1200 and ".5" -> 50 exact
 *  (floating point multiplication is not trustworthy here). */
function parseMoney(text: string): number | null {
  if (!MONEY_RE.test(text)) return null;
  const [whole, frac = ""] = text.split(".");
  return Number(whole || "0") * 100 + Number(frac.padEnd(2, "0").slice(0, 2));
}

export default function BudgetRebalanceModal({
  open,
  budgets,
  canSuggest,
  onApplied,
  onClose,
}: Props) {
  const money = useMoney();
  const balancesHidden = useBalancesHidden();

  const [rows, setRows] = useState<Row[]>([]);
  const [text, setText] = useState<Record<number, string>>({});
  // Armed after a 409/404 + a successful `onApplied()` reload. Not a simple
  // boolean *state* flag: the reload's own state update (in the parent) and
  // this arm can land in separate renders, so we wait for the NEXT `budgets`
  // prop to actually change rather than rebuilding from whatever (possibly
  // still-stale) `budgets` closure this render captured.
  const pendingResnapshot = useRef(false);

  const [suggesting, setSuggesting] = useState(false);
  const [suggestError, setSuggestError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [applyError, setApplyError] = useState("");

  const dialogRef = useRef<HTMLDivElement>(null);

  function buildSnapshot() {
    const nextRows: Row[] = budgets.map((b) => {
      const baseCents = Math.round(toNumber(b.amount) * 100);
      return {
        id: b.id,
        category_id: b.category_id,
        category_name: b.category_name,
        baseCents,
        baseText: centsToText(baseCents),
      };
    });
    const nextText: Record<number, string> = {};
    for (const r of nextRows) nextText[r.id] = r.baseText;
    setRows(nextRows);
    setText(nextText);
    setApplyError("");
    setSuggestError("");
  }

  // Snapshot ONLY on open, never on a `budgets` prop change while open — a
  // background reload must not add/hide a row or reset a typed value.
  useEffect(() => {
    if (!open) return;
    buildSnapshot();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- snapshot on open only, deliberately not on every `budgets` change
  }, [open]);

  // Explicit re-snapshot, used only after a 409/404 has been resolved by the
  // parent reloading (`onApplied`) with the now-committed amounts. Fires on
  // the next `budgets` prop change while armed, then disarms.
  useEffect(() => {
    if (!pendingResnapshot.current) return;
    buildSnapshot();
    pendingResnapshot.current = false;
    // eslint-disable-next-line react-hooks/exhaustive-deps -- rebuilds from whichever `budgets` just changed
  }, [budgets]);

  useFocusTrap({ active: open, containerRef: dialogRef });

  useEffect(() => {
    if (!open) return;
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [open, onClose]);

  const parsedById = useMemo(() => {
    const m = new Map<number, number | null>();
    for (const r of rows) m.set(r.id, parseMoney(text[r.id] ?? ""));
    return m;
  }, [rows, text]);

  const allValid = rows.length > 0 && rows.every((r) => parsedById.get(r.id) !== null);
  const anyChanged = rows.some((r) => text[r.id] !== r.baseText);
  const netCentsExact = allValid
    ? rows.reduce((s, r) => s + ((parsedById.get(r.id) as number) - r.baseCents), 0)
    : null;
  // Display net always resolves (invalid rows contribute zero change), so the
  // footer amount line never shows NaN while the user is mid-edit.
  const displayNetCents = rows.reduce((s, r) => {
    const p = parsedById.get(r.id);
    return s + ((p ?? r.baseCents) - r.baseCents);
  }, 0);

  const applyEnabled =
    allValid && anyChanged && netCentsExact === 0 && !submitting;

  let statusWord: string;
  if (!allValid) statusWord = "Fix the highlighted amounts";
  else if (!anyChanged) statusWord = "No changes yet";
  else if (netCentsExact === 0) statusWord = "Balanced";
  else statusWord = `Not balanced: must net to ${money(0)}`;

  function setRowText(id: number, value: string) {
    setText((prev) => ({ ...prev, [id]: value }));
  }

  function handleReset() {
    const next: Record<number, string> = {};
    for (const r of rows) next[r.id] = r.baseText;
    setText(next);
    setApplyError("");
  }

  async function handleSuggest() {
    setSuggestError("");
    setSuggesting(true);
    try {
      const res = await apiFetch<RebalanceResponse>("/api/v1/ai/budget/rebalance", {
        method: "POST",
      });
      if (res && res.status === "ok") {
        setText((prev) => {
          const next = { ...prev };
          for (const s of res.suggestions ?? []) {
            const row = rows.find((r) => r.category_id === s.category_id);
            if (row) next[row.id] = toNumber(s.suggested_amount).toFixed(2);
          }
          return next;
        });
      } else if (res) {
        setSuggestError(maskMoneyText(res.summary ?? ""));
      }
    } catch (err) {
      setSuggestError(extractErrorMessage(err));
    } finally {
      setSuggesting(false);
    }
  }

  async function handleApply() {
    if (!applyEnabled) return;
    setSubmitting(true);
    setApplyError("");
    const items = rows
      .filter((r) => text[r.id] !== r.baseText)
      .map((r) => ({
        budget_id: r.id,
        expected_amount: r.baseText,
        amount: centsToText(parsedById.get(r.id) as number),
      }));
    try {
      await apiFetch("/api/v1/budgets/rebalance", {
        method: "POST",
        body: JSON.stringify({ items }),
      });
      await onApplied();
      onClose();
    } catch (err) {
      if (err instanceof ApiResponseError && (err.status === 409 || err.status === 404)) {
        setApplyError(RECONCILE_MESSAGE);
        try {
          await onApplied();
          pendingResnapshot.current = true;
        } catch {
          setApplyError(RECONCILE_FAILED_MESSAGE);
        }
      } else {
        setApplyError(extractErrorMessage(err));
      }
    } finally {
      setSubmitting(false);
    }
  }

  if (!open) return null;

  const poolCents = rows.reduce((s, r) => s + r.baseCents, 0);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="rebalance-title"
    >
      <div
        ref={dialogRef}
        className={`${card} relative w-full max-w-3xl max-h-[90vh] overflow-y-auto`}
      >
        <div className="flex items-start justify-between border-b border-border-subtle px-6 py-4">
          <div>
            <h2 id="rebalance-title" className="text-base font-semibold text-text-primary">
              Rebalance budgets
            </h2>
            <p className="mt-1 text-xs text-text-muted">
              Move amounts between budgets. The total must stay the same.
            </p>
          </div>
        </div>

        <div className="px-6 py-5">
          {balancesHidden ? (
            <div className="py-10 text-center">
              <p className="text-sm text-text-primary">Show balances to rebalance.</p>
              <button
                type="button"
                onClick={() => import("@/lib/format").then((m) => m.setBalancesHidden(false))}
                className={`${btnSecondary} mt-4`}
              >
                Show balances
              </button>
            </div>
          ) : (
            <>
              {suggestError && (
                <div className={`mb-4 ${errorCls}`} role="alert">
                  {suggestError}
                </div>
              )}
              <div className="space-y-3">
                {rows.map((row) => {
                  const rawText = text[row.id] ?? "";
                  const parsed = parsedById.get(row.id);
                  const invalid = parsed === null;
                  const displayCents = parsed ?? row.baseCents;
                  const deltaCents = displayCents - row.baseCents;
                  const nameId = `rb-name-${row.id}`;
                  const amtId = `rb-amt-${row.id}`;
                  const allocId = `rb-alloc-${row.id}`;
                  const errId = `rb-err-${row.id}`;
                  return (
                    <div
                      key={row.id}
                      className="rounded-md border border-border-subtle bg-surface px-4 py-3"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span id={nameId} className="text-sm text-text-primary">
                          {row.category_name}
                        </span>
                        <span className="text-xs tabular-nums text-text-secondary">
                          {deltaCents >= 0 ? "+" : ""}
                          {money(deltaCents / 100)}
                        </span>
                      </div>
                      <span id={amtId} className="sr-only">
                        amount
                      </span>
                      <span id={allocId} className="sr-only">
                        allocation
                      </span>
                      <p className="mt-1 text-xs tabular-nums text-text-muted">
                        {money(row.baseCents / 100)} → {money(displayCents / 100)}
                      </p>
                      <input
                        type="range"
                        min={0}
                        max={poolCents / 100}
                        step={0.01}
                        value={displayCents / 100}
                        aria-labelledby={`${nameId} ${allocId}`}
                        aria-valuetext={money(displayCents / 100)}
                        onChange={(e) => setRowText(row.id, Number(e.target.value).toFixed(2))}
                        className="mt-2 w-full accent-border-strong"
                      />
                      <input
                        type="text"
                        inputMode="decimal"
                        autoComplete="off"
                        aria-labelledby={`${nameId} ${amtId}`}
                        aria-invalid={invalid ? "true" : undefined}
                        aria-describedby={invalid ? errId : undefined}
                        value={rawText}
                        onChange={(e) => setRowText(row.id, e.target.value)}
                        className={`mt-2 w-full sm:w-32 ${inputCls} ${invalid ? "border-danger" : ""}`}
                      />
                      {invalid && (
                        <p id={errId} className="mt-1 text-xs text-danger">
                          {ERROR_INVALID}
                        </p>
                      )}
                    </div>
                  );
                })}
              </div>

              {applyError && (
                <div className={`mt-4 ${errorCls}`} role="alert">
                  {applyError}
                </div>
              )}
            </>
          )}
        </div>

        <div className="sticky bottom-0 flex flex-wrap items-center justify-between gap-3 border-t border-border-subtle bg-surface px-6 py-3">
          <div>
            {!balancesHidden && (
              <>
                <p className="text-xs text-text-secondary tabular-nums">
                  Net change {displayNetCents >= 0 ? "+" : ""}
                  {money(displayNetCents / 100)}
                </p>
                <p role="status" className="text-xs text-text-muted">
                  {statusWord}
                </p>
              </>
            )}
          </div>
          <div className="flex flex-wrap justify-end gap-2">
            {!balancesHidden && canSuggest && (
              <button
                type="button"
                onClick={handleSuggest}
                disabled={suggesting}
                className={btnSecondary}
              >
                {suggesting ? "Loading..." : "Use suggestions"}
              </button>
            )}
            {!balancesHidden && (
              <button type="button" onClick={handleReset} className={btnSecondary}>
                Reset
              </button>
            )}
            <button type="button" onClick={onClose} className={btnSecondary}>
              Cancel
            </button>
            {!balancesHidden && (
              <button
                type="button"
                onClick={handleApply}
                disabled={!applyEnabled}
                className={btnPrimary}
              >
                Apply
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
