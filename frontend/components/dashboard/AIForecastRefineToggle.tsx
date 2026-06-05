"use client";

import { useState } from "react";
import { Sparkles, AlertTriangle } from "lucide-react";
import { ApiResponseError } from "@/lib/api";
import { card } from "@/lib/styles";
import { AIForecastRefinePanel } from "@/components/dashboard/AIForecastRefinePanel";
import { useAiStatus } from "@/lib/hooks/use-ai-status";
import { useAuth } from "@/components/auth/AuthProvider";
import { SetUpAiCta } from "@/components/ai/SetUpAiCta";
import HelpTooltip from "@/components/help/HelpTooltip";

// Friendly copy for the typed backend fallback_reason codes, so the badge
// never shows a raw code like "ai_response_invalid_schema" to the user.
const FALLBACK_REASON_COPY: Record<string, string> = {
  ai_response_invalid_schema: "The AI response could not be applied this time, showing your baseline.",
  ai_structured_output_failed: "The AI response could not be read, showing your baseline.",
  ai_routing_not_configured: "Configure an AI provider in Settings to use this.",
  ai_cap_exceeded: "Your AI usage limit for this period has been reached.",
  ai_capability_not_supported: "The selected AI provider does not support this feature.",
  ai_native_not_available: "The built-in AI provider is not available yet.",
  insufficient_history: "Not enough history yet to analyze.",
  history_build_failed: "Could not load your history, showing your baseline.",
};

function fallbackReasonLabel(reason: string | null): string {
  if (!reason) return "Showing your baseline.";
  if (FALLBACK_REASON_COPY[reason]) return FALLBACK_REASON_COPY[reason];
  // ai_dispatch_failed:<code> and any unmapped code -> generic, never raw.
  return "AI is temporarily unavailable, showing your baseline.";
}

// Mirrors backend/app/schemas/ai_forecast.RefinedForecastResponse.
export interface RefinedCategoryRow {
  category_id: number;
  category_name: string;
  baseline_forecast: string;
  multiplier: number;
  refined_forecast: string;
}

export interface AnomalyFlag {
  category_id: number | null;
  category_name: string;
  description: string;
  severity: "info" | "warning" | "alert";
}

export interface RefinedForecastProvenance {
  ai_applied: boolean;
  fallback_reason: string | null;
  model: string | null;
  confidence: number | null;
  summary: string | null;
  notes: string[];
}

export interface RefinedForecastResponse {
  period_start: string;
  period_end: string;
  baseline_forecast_expense: string;
  refined_forecast_expense: string;
  baseline_forecast_income: string;
  refined_forecast_income: string;
  categories: RefinedCategoryRow[];
  anomalies: AnomalyFlag[];
  provenance: RefinedForecastProvenance;
}

export interface AIForecastRefineToggleProps {
  periodStart: string | null;
  // Render-only render-prop: parent decides whether to surface the
  // toggle (e.g. only when the OnTrackTile has a plan). Default true.
  visible?: boolean;
}

/**
 * AI-refined forecast toggle + badge.
 *
 * Opt-in: clicking "Apply AI refinement" hits
 * `POST /api/v1/ai/forecast/refine` and surfaces a small badge with
 * the delta vs. the baseline. If the feature gate is closed (403), the
 * toggle hides itself silently — the user never sees a permission
 * error, they just never see the AI option.
 *
 * On any other failure (no routing, cap exceeded, structured output
 * exhausted) the backend falls back to the baseline; the UI surfaces
 * the typed `fallback_reason` in the tooltip so the user understands
 * why no adjustments were applied.
 */
export default function AIForecastRefineToggle({
  periodStart,
  visible = true,
}: AIForecastRefineToggleProps) {
  const [refined, setRefined] = useState<RefinedForecastResponse | null>(null);
  const [panelOpen, setPanelOpen] = useState(false);
  const [gateBlocked, setGateBlocked] = useState(false);
  const [tooltipOpen, setTooltipOpen] = useState(false);

  const ai = useAiStatus();
  const forecastAi = ai?.forecast;
  const { user } = useAuth();
  const role = user?.role ?? null;

  if (!visible || gateBlocked) return null;

  // Fail CLOSED, consistent with the budgets + transactions surfaces: while
  // useAiStatus() is still resolving (undefined), `!forecastAi?.entitled` is
  // true, so we render nothing until the gating signal is known (no flash of
  // the live toggle for non-entitled orgs).
  if (!forecastAi?.entitled) return null;
  if (!forecastAi.configured) {
    return (
      <SetUpAiCta
        role={role}
        className="rounded border border-border bg-bg-secondary px-3 py-1.5 text-xs font-medium text-text-primary hover:bg-bg-tertiary disabled:opacity-50"
      />
    );
  }

  // When the panel calls onApplied (after a successful Confirm), store the
  // result and close the panel so the refined-result view renders below.
  const handleApplied = (result: RefinedForecastResponse) => {
    setRefined(result);
    setPanelOpen(false);
  };

  // The panel's estimate call may 403 (feature gate closed) — propagate that
  // up to hide the toggle exactly as the old direct-call path did.
  const handleGateBlock = (err: unknown) => {
    if (err instanceof ApiResponseError && err.status === 403) {
      setGateBlocked(true);
    }
  };

  // Idle state — invite the user to configure and confirm.
  if (refined === null) {
    if (panelOpen) {
      return (
        <AIForecastRefinePanel
          periodStart={periodStart}
          onApplied={handleApplied}
          onCancel={() => setPanelOpen(false)}
          onGateBlock={handleGateBlock}
        />
      );
    }
    return (
      <div className={`${card} mt-3 flex items-center gap-3 p-3 md:p-4`}>
        <Sparkles className="h-4 w-4 text-text-secondary" aria-hidden="true" />
        <div className="flex-1 text-sm text-text-secondary">
          Layer AI-detected seasonality on this forecast.
        </div>
        <button
          type="button"
          onClick={() => setPanelOpen(true)}
          data-testid="ai-forecast-refine-toggle"
          className="rounded border border-border bg-bg-secondary px-3 py-1.5 text-xs font-medium text-text-primary hover:bg-bg-tertiary disabled:opacity-50"
        >
          Refine forecast with AI
        </button>
        <HelpTooltip k="ai.forecast" />
      </div>
    );
  }

  // Refined state — show badge + delta.
  const aiApplied = refined.provenance.ai_applied;
  const baseline = Number(refined.baseline_forecast_expense);
  const refinedAmt = Number(refined.refined_forecast_expense);
  const delta = refinedAmt - baseline;
  const adjustments = refined.categories.filter((c) => c.multiplier !== 1);

  return (
    <div className={`${card} mt-3 p-3 md:p-4`} data-testid="ai-forecast-refined-panel">
      <div className="flex items-center gap-3">
        {aiApplied ? (
          <span
            data-testid="ai-refined-badge"
            className="inline-flex items-center gap-1 rounded-full bg-bg-tertiary px-2 py-0.5 text-xs font-medium text-text-primary"
          >
            <Sparkles className="h-3 w-3" aria-hidden="true" />
            AI-refined
          </span>
        ) : (
          <span
            data-testid="ai-fallback-badge"
            className="inline-flex items-center gap-1 rounded-full bg-bg-tertiary px-2 py-0.5 text-xs font-medium text-text-muted"
          >
            <AlertTriangle className="h-3 w-3" aria-hidden="true" />
            Baseline (AI unavailable)
          </span>
        )}
        <div className="flex-1 text-sm text-text-secondary">
          {aiApplied ? (
            <>
              {delta >= 0 ? "+" : ""}
              {delta.toFixed(2)} vs. baseline
              {adjustments.length > 0 && (
                <span className="ml-2 text-xs text-text-muted">
                  ({adjustments.length} categor
                  {adjustments.length === 1 ? "y" : "ies"} adjusted)
                </span>
              )}
            </>
          ) : (
            <span data-testid="ai-fallback-reason" className="text-xs">
              {fallbackReasonLabel(refined.provenance.fallback_reason)}
            </span>
          )}
        </div>
        <button
          type="button"
          onClick={() => setTooltipOpen((open) => !open)}
          aria-expanded={tooltipOpen}
          aria-controls="ai-forecast-refine-tooltip"
          className="text-xs text-text-secondary underline underline-offset-2 hover:text-text-primary"
        >
          {tooltipOpen ? "Hide details" : "What changed?"}
        </button>
      </div>
      {tooltipOpen && (
        <div
          id="ai-forecast-refine-tooltip"
          role="region"
          aria-label="AI refinement details"
          className="mt-3 border-t border-border pt-3 text-xs text-text-secondary"
        >
          {aiApplied && refined.provenance.summary && (
            <p className="mb-2 italic">{refined.provenance.summary}</p>
          )}
          {adjustments.length > 0 && (
            <ul className="mb-2 list-disc space-y-1 pl-4" data-testid="ai-adjustments-list">
              {adjustments.map((adj) => (
                <li key={adj.category_id}>
                  <span className="font-medium text-text-primary">
                    {adj.category_name}
                  </span>
                  : {Number(adj.baseline_forecast).toFixed(2)} -&gt;{" "}
                  {Number(adj.refined_forecast).toFixed(2)} (x
                  {adj.multiplier.toFixed(2)})
                </li>
              ))}
            </ul>
          )}
          {refined.anomalies.length > 0 && (
            <div className="mb-2">
              <p className="font-medium text-text-primary">Anomalies</p>
              <ul className="mt-1 list-disc space-y-1 pl-4">
                {refined.anomalies.map((anom, idx) => (
                  <li key={`${anom.category_id ?? "general"}-${idx}`}>
                    <span className="font-medium text-text-primary">
                      {anom.category_name}
                    </span>
                    : {anom.description}{" "}
                    <span className="text-text-muted">[{anom.severity}]</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {refined.provenance.notes.length > 0 && (
            <ul className="mb-2 list-disc space-y-1 pl-4 text-text-muted">
              {refined.provenance.notes.map((note, idx) => (
                <li key={idx}>{note}</li>
              ))}
            </ul>
          )}
          {refined.provenance.model && (
            <p className="text-text-muted">
              Model: {refined.provenance.model}
              {refined.provenance.confidence !== null &&
                refined.provenance.confidence !== undefined && (
                  <>
                    {", confidence "}
                    {(refined.provenance.confidence * 100).toFixed(0)}%
                  </>
                )}
            </p>
          )}
          <button
            type="button"
            onClick={() => {
              setRefined(null);
              setTooltipOpen(false);
            }}
            className="mt-3 text-xs text-text-secondary underline underline-offset-2 hover:text-text-primary"
          >
            Revert to baseline
          </button>
        </div>
      )}
    </div>
  );
}
