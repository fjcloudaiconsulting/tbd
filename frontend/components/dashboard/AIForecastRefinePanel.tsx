"use client";

import { useCallback, useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { apiFetch, ApiResponseError } from "@/lib/api";
import { card } from "@/lib/styles";
import type { ForecastRefineEstimate } from "@/lib/types";
import type { RefinedForecastResponse } from "@/components/dashboard/AIForecastRefineToggle";

const TIMEFRAMES = [3, 6, 12] as const;
const SCOPES = [
  { value: "top_10", label: "Top 10 categories" },
  { value: "top_20", label: "Top 20 categories" },
  { value: "all", label: "All categories" },
] as const;

const REASON_COPY: Record<string, string> = {
  ai_routing_not_configured:
    "Configure an AI provider in Settings to use this feature.",
  insufficient_history: "Not enough transaction history yet to analyze.",
};

export interface AIForecastRefinePanelProps {
  periodStart?: string | null;
  onApplied: (result: RefinedForecastResponse) => void;
  onCancel?: () => void;
  /** Called with the error when a 403 is received (feature gate closed). */
  onGateBlock?: (err: unknown) => void;
}

/**
 * Configure, estimate, and confirm panel for AI forecast refinement.
 *
 * On mount (and on any select change), posts to /estimate to show the
 * user an approximate cost and duration before spending any tokens.
 * The Confirm button is disabled while estimating, while the refine
 * call is in flight, or when can_proceed is false (e.g. no AI provider
 * configured). When confirmed, calls onApplied with the refined result
 * so the parent toggle can render the result using its existing path.
 */
export function AIForecastRefinePanel({
  periodStart,
  onApplied,
  onCancel,
  onGateBlock,
}: AIForecastRefinePanelProps) {
  const [timeframe, setTimeframe] = useState<number>(6);
  const [scope, setScope] = useState<string>("top_20");
  const [estimate, setEstimate] = useState<ForecastRefineEstimate | null>(null);
  const [estimating, setEstimating] = useState(false);
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);

  const refreshEstimate = useCallback(async () => {
    setEstimating(true);
    setEstimate(null);
    try {
      const est = await apiFetch<ForecastRefineEstimate>(
        "/api/v1/ai/forecast/refine/estimate",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            period_start: periodStart ?? null,
            timeframe_months: timeframe,
            scope,
          }),
        },
      );
      setEstimate(est);
    } catch (err) {
      // 403 = feature gate closed; propagate to parent for hide logic.
      if (err instanceof ApiResponseError && err.status === 403) {
        onGateBlock?.(err);
        return;
      }
      // Other estimation errors are non-fatal; the Confirm button stays disabled.
      setEstimate(null);
    } finally {
      setEstimating(false);
    }
  }, [timeframe, scope, periodStart]);

  useEffect(() => {
    void refreshEstimate();
  }, [refreshEstimate]);

  const handleConfirm = async () => {
    setRunning(true);
    setRunError(null);
    try {
      const refined = await apiFetch<RefinedForecastResponse>(
        "/api/v1/ai/forecast/refine",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            period_start: periodStart ?? null,
            timeframe_months: timeframe,
            scope,
          }),
        },
      );
      onApplied(refined);
    } catch (err) {
      if (err instanceof ApiResponseError && err.status === 403) {
        onGateBlock?.(err);
        return;
      }
      setRunError(err instanceof Error ? err.message : "Failed to refine forecast");
    } finally {
      setRunning(false);
    }
  };

  const canProceed = !!estimate?.can_proceed;
  const dollars = estimate
    ? `$${(estimate.est_cost_cents / 100).toFixed(2)}`
    : null;

  return (
    <div className={`${card} mt-3 p-3 md:p-4`} data-testid="ai-forecast-refine-panel">
      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-4">
          <div className="flex flex-col gap-1">
            <label
              htmlFor="ai-refine-timeframe"
              className="text-xs font-medium text-text-muted"
            >
              Timeframe
            </label>
            <select
              id="ai-refine-timeframe"
              value={timeframe}
              onChange={(e) => setTimeframe(Number(e.target.value))}
              className="rounded border border-border bg-bg-secondary px-2 py-1 text-xs text-text-primary focus:border-accent focus:outline-none"
            >
              {TIMEFRAMES.map((m) => (
                <option key={m} value={m}>
                  {m} months
                </option>
              ))}
            </select>
          </div>
          <div className="flex flex-col gap-1">
            <label
              htmlFor="ai-refine-scope"
              className="text-xs font-medium text-text-muted"
            >
              Scope
            </label>
            <select
              id="ai-refine-scope"
              value={scope}
              onChange={(e) => setScope(e.target.value)}
              className="rounded border border-border bg-bg-secondary px-2 py-1 text-xs text-text-primary focus:border-accent focus:outline-none"
            >
              {SCOPES.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div className="text-xs text-text-secondary" aria-live="polite">
          {estimating ? (
            <span className="inline-flex items-center gap-1">
              <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
              Estimating cost…
            </span>
          ) : estimate ? (
            <span>
              {"≈"} {dollars} {"·"} {estimate.est_output_tokens} tokens{" "}
              {"·"} {estimate.duration_band}
            </span>
          ) : null}
        </div>

        {estimate && !canProceed && estimate.reason && (
          <p className="text-xs text-text-muted" role="status">
            {REASON_COPY[estimate.reason] ?? "This feature is currently unavailable."}
          </p>
        )}

        {runError && (
          <p className="text-xs text-danger" role="status">
            {runError}
          </p>
        )}

        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={handleConfirm}
            disabled={!canProceed || estimating || running}
            className="rounded border border-border bg-bg-secondary px-3 py-1.5 text-xs font-medium text-text-primary hover:bg-bg-tertiary disabled:opacity-50"
          >
            {running ? (
              <span className="inline-flex items-center gap-1">
                <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
                Refining…
              </span>
            ) : (
              "Confirm"
            )}
          </button>
          {onCancel && (
            <button
              type="button"
              onClick={onCancel}
              className="text-xs text-text-secondary underline underline-offset-2 hover:text-text-primary"
            >
              Cancel
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
