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
import HelpTooltip from "@/components/help/HelpTooltip";
import {
  btnPrimary,
  btnSecondary,
  card,
  error as errorCls,
  input as inputCls,
  warning as warningCls,
} from "@/lib/styles";
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
  uncovered_overspend?: string | number;
}

// Old modal's friendly empty-state titles (TBD-461 restore), keyed by the
// non-ok statuses the backend can return. The raw `summary` is still shown
// underneath, but the headline is never the bare status string.
const STATUS_TITLES: Record<Exclude<RebalanceStatus, "ok">, string> = {
  empty_no_budgets: "No budgets yet",
  empty_no_history: "Not enough history yet",
  empty_no_surplus: "Nothing to reallocate",
  llm_unavailable: "AI is unavailable",
};

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

// Numeric(12,2): up to 10 integer digits, up to 2 decimals.
const MONEY_RE = /^(\d{1,10}(\.\d{0,2})?|\.\d{1,2})$/;

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
  // Bumped after a 409/404 + a successful `onApplied()` reload, to force a
  // re-snapshot from whatever `budgets` this render sees. A ref-based "arm
  // and wait for the next `budgets` prop change" cannot work: if the parent
  // already committed the reloaded budgets before this code runs (e.g. it
  // updates state synchronously inside `onApplied`, before the awaited
  // promise resolves), the `budgets` prop never changes *again* afterward,
  // so an effect keyed on `[budgets]` never re-fires. A state bump always
  // forces one more render of this component, which re-reads the current
  // `budgets` prop regardless of when it last changed.
  const [resnapshotToken, setResnapshotToken] = useState(0);
  // Bumped on every open, so an in-flight 409 reload can tell whether the
  // modal it belongs to is still the one on screen.
  const openGeneration = useRef(0);

  const [suggesting, setSuggesting] = useState(false);
  const [suggestError, setSuggestError] = useState("");
  // Non-ok status from the last suggest fetch, rendered via STATUS_TITLES
  // instead of the raw `summary` string (R4). Cleared alongside suggestError.
  const [suggestStatus, setSuggestStatus] = useState<Exclude<RebalanceStatus, "ok"> | null>(null);
  // Set only on an "ok" response (R1/R2): the AI summary + uncovered-overspend
  // figure, shown above the rows until Reset or a re-snapshot.
  const [aiSummary, setAiSummary] = useState("");
  const [uncoveredOverspend, setUncoveredOverspend] = useState(0);
  // Per-row reasoning text from the last suggestion fetch (R3), keyed by row
  // id. Rows with no matching suggestion have no entry.
  const [reasoningById, setReasoningById] = useState<Record<number, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [applyError, setApplyError] = useState("");

  const dialogRef = useRef<HTMLDivElement>(null);

  function buildSnapshot(opts: { keepApplyError?: boolean } = {}) {
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
    // On the reconcile path the "Reloaded the latest amounts" message must
    // survive the re-snapshot it is reporting on; Reset and opening fresh
    // still clear it.
    if (!opts.keepApplyError) setApplyError("");
    setSuggestError("");
    setSuggestStatus(null);
    setAiSummary("");
    setUncoveredOverspend(0);
    setReasoningById({});
  }

  // Snapshot ONLY on open, never on a `budgets` prop change while open — a
  // background reload must not add/hide a row or reset a typed value.
  useEffect(() => {
    if (!open) return;
    openGeneration.current += 1;
    buildSnapshot();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- snapshot on open only, deliberately not on every `budgets` change
  }, [open]);

  // Explicit re-snapshot, used only after a 409/404 has been resolved by the
  // parent reloading (`onApplied`) with the now-committed amounts. `token`
  // starting at 0 means "never armed" so this never fires on mount.
  useEffect(() => {
    if (resnapshotToken === 0) return;
    buildSnapshot({ keepApplyError: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- rebuilds from whichever `budgets` this render sees
  }, [resnapshotToken]);

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
  // Compare parsed cents, not raw text: "12.5" and a base of "12.50" are the
  // same amount and must not count as a change (or be POSTed).
  const isRowChanged = (r: Row) => {
    const p = parsedById.get(r.id);
    return p !== null && p !== r.baseCents;
  };
  const anyChanged = rows.some(isRowChanged);
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
    setSuggestError("");
    setSuggestStatus(null);
    setAiSummary("");
    setUncoveredOverspend(0);
    setReasoningById({});
  }

  async function handleSuggest() {
    setSuggestError("");
    setSuggestStatus(null);
    setAiSummary("");
    setUncoveredOverspend(0);
    setReasoningById({});
    setSuggesting(true);
    try {
      const res = await apiFetch<RebalanceResponse>("/api/v1/ai/budget/rebalance", {
        method: "POST",
      });
      if (res && res.status === "ok") {
        // Resolve matches OUTSIDE the updater: React runs updaters at the next
        // render, so a counter incremented inside one still reads 0 here.
        const hits = (res.suggestions ?? []).flatMap((s) => {
          const row = rows.find((r) => r.category_id === s.category_id);
          return row ? [{ id: row.id, text: toNumber(s.suggested_amount).toFixed(2), reasoning: s.reasoning }] : [];
        });
        setText((prev) => {
          const next = { ...prev };
          for (const h of hits) next[h.id] = h.text;
          return next;
        });
        const nextReasoning: Record<number, string> = {};
        for (const h of hits) if (h.reasoning) nextReasoning[h.id] = h.reasoning;
        setReasoningById(nextReasoning);
        setAiSummary(res.summary ?? "");
        setUncoveredOverspend(Number(res.uncovered_overspend ?? 0));
        if (hits.length === 0) {
          setSuggestError(maskMoneyText("No suggestions matched a budget in this period."));
        }
      } else if (res) {
        setSuggestStatus(res.status as Exclude<RebalanceStatus, "ok">);
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
      .filter(isRowChanged)
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
    } catch (err) {
      if (err instanceof ApiResponseError && (err.status === 409 || err.status === 404)) {
        setApplyError(RECONCILE_MESSAGE);
        const gen = openGeneration.current;
        try {
          await onApplied();
          // A close/reopen during the reload started a fresh snapshot; a late
          // bump would overwrite the user's new edits with this stale reload.
          if (gen === openGeneration.current) setResnapshotToken((t) => t + 1);
        } catch {
          setApplyError(RECONCILE_FAILED_MESSAGE);
        }
      } else {
        setApplyError(extractErrorMessage(err));
      }
      setSubmitting(false);
      return;
    }
    // The POST succeeded: the change is applied regardless of what happens
    // next. A failing reload (`onApplied`) is a "list may be stale" problem
    // for the parent, never an Apply failure — close either way.
    try {
      await onApplied();
    } catch {
      // Parent's reload failed; the write already committed on the server.
    }
    setSubmitting(false);
    onClose();
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
              {suggestStatus ? (
                <div className="mb-4" data-testid="rebalance-empty-state">
                  <p className="text-sm font-medium text-text-primary">
                    {STATUS_TITLES[suggestStatus] ?? "Nothing to rebalance"}
                  </p>
                  {suggestError && <p className="mt-1 text-xs text-text-muted">{suggestError}</p>}
                </div>
              ) : (
                suggestError && (
                  <div className={`mb-4 ${errorCls}`} role="alert">
                    {suggestError}
                  </div>
                )
              )}
              {aiSummary && (
                <p className="mb-4 text-sm text-text-secondary" data-testid="rebalance-summary">
                  {maskMoneyText(aiSummary)}
                </p>
              )}
              {uncoveredOverspend > 0 && (
                <div className={`mb-4 ${warningCls}`} data-testid="rebalance-uncovered" role="status">
                  You&apos;re {money(uncoveredOverspend)} over plan this period. Spending
                  exceeds your total budget, so not every category could be fully covered.
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
                        className="mt-2 min-h-[44px] w-full accent-border-strong sm:min-h-0"
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
                      {reasoningById[row.id] && (
                        <p className="mt-1 text-xs text-text-secondary">
                          {maskMoneyText(reasoningById[row.id])}
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
              <span className="inline-flex items-center gap-1">
                <button
                  type="button"
                  onClick={handleSuggest}
                  disabled={suggesting}
                  className={btnSecondary}
                >
                  {suggesting ? "Loading..." : "Use suggestions"}
                </button>
                <HelpTooltip k="ai.budget" />
              </span>
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
