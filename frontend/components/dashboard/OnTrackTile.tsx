"use client";

import Link from "next/link";
import { AlertCircle, AlertTriangle, Check, RefreshCw } from "lucide-react";
import { btnSecondary, card } from "@/lib/styles";
import { formatAmount } from "@/lib/format";

export interface ForecastPlanLike {
  total_planned_expense: string | number;
}

export interface ForecastProjectionLike {
  executed_expense: string | number;
  forecast_expense: string | number;
}

export interface OnTrackTileProps {
  forecastPlan: ForecastPlanLike | null;
  projection: ForecastProjectionLike | null;
  projectionFailed: boolean;
  projectionLoading: boolean;
  onRetryProjection: () => void;
  isPastPeriod: boolean;
  isFuturePeriod: boolean;
}

// Verdict bands: ≤95% on track, 95-105% watch, >105% over.
// Tunable in a follow-up; for now constants live here.
const ON_TRACK_MAX = 0.95;
const WATCH_MAX = 1.05;

type Verdict = "on-track" | "watch" | "over";

function computeVerdict(pct: number): Verdict {
  if (pct <= ON_TRACK_MAX) return "on-track";
  if (pct <= WATCH_MAX) return "watch";
  return "over";
}

const CURRENT_LABELS: Record<Verdict, string> = {
  "on-track": "ON TRACK",
  watch: "WATCH",
  over: "OVER BUDGET",
};

const PAST_LABELS: Record<Verdict, string> = {
  "on-track": "ENDED ON TRACK",
  watch: "ENDED ON WATCH",
  over: "ENDED OVER BUDGET",
};

const VERDICT_COLOR: Record<Verdict, string> = {
  "on-track": "text-success",
  watch: "text-text-primary",
  over: "text-danger",
};

function VerdictIcon({ verdict }: { verdict: Verdict }) {
  const Icon = verdict === "on-track" ? Check : verdict === "watch" ? AlertCircle : AlertTriangle;
  return <Icon className="h-6 w-6" aria-hidden="true" />;
}

function Stat({
  label,
  value,
  sublabel,
  valueClass = "text-text-primary",
  muted = false,
}: {
  label: string;
  value: string;
  sublabel?: string;
  valueClass?: string;
  muted?: boolean;
}) {
  return (
    <div>
      <p className="text-xs font-semibold uppercase tracking-[0.08em] text-text-muted">{label}</p>
      <p
        className={`mt-1 text-2xl font-semibold tabular-nums ${
          muted ? "text-text-muted" : valueClass
        }`}
      >
        {value}
      </p>
      {sublabel && <p className="mt-1 text-xs text-text-muted">{sublabel}</p>}
    </div>
  );
}

export default function OnTrackTile({
  forecastPlan,
  projection,
  projectionFailed,
  projectionLoading,
  onRetryProjection,
  isPastPeriod,
  isFuturePeriod,
}: OnTrackTileProps) {
  const plannedExpense = forecastPlan ? Number(forecastPlan.total_planned_expense) : 0;
  const hasPlan = forecastPlan !== null && plannedExpense > 0;

  // Past period with no plan: non-actionable, past-tense copy.
  // Runs BEFORE the no-plan branch so a closed period without a plan
  // doesn't render the current-period "Set one up" CTA.
  if (isPastPeriod && !hasPlan) {
    return (
      <section
        className={`${card} p-6 md:p-8`}
        data-testid="on-track-tile"
        aria-label="No plan was set for this period"
      >
        <header className="mb-6 flex items-center justify-between">
          <span className="text-xs font-semibold uppercase tracking-[0.08em] text-text-muted">
            Plan vs Actual
          </span>
          <span className="text-xs text-text-secondary">Past period</span>
        </header>
        <p className="text-sm text-text-muted">No plan was set for this period.</p>
      </section>
    );
  }

  // Pre-period (selected period in the future): plan if drafted, suppress everything else.
  if (isFuturePeriod) {
    return (
      <section className={`${card} p-6 md:p-8`} data-testid="on-track-tile" aria-label="Plan ahead">
        <header className="mb-6 flex items-center justify-between">
          <span className="text-xs font-semibold uppercase tracking-[0.08em] text-text-muted">
            Plan ahead
          </span>
          <span className="text-xs text-text-secondary">Future period</span>
        </header>
        <div className="grid grid-cols-1 gap-6 md:grid-cols-2 lg:grid-cols-4">
          <Stat
            label="PLAN"
            value={hasPlan ? formatAmount(plannedExpense) : "—"}
            sublabel={hasPlan ? "full month" : "not yet planned"}
            muted={!hasPlan}
          />
          <Stat label="SPENT" value="—" sublabel="nothing yet" muted />
          <Stat label="VARIANCE" value="—" muted />
          <Stat label="PROJECTED" value="—" muted />
        </div>
        <div className="mt-6 text-sm">
          <Link
            href="/forecast-plans"
            className="text-text-primary underline underline-offset-2 hover:text-text-secondary"
          >
            Plan ahead →
          </Link>
        </div>
      </section>
    );
  }

  // Current period, no plan exists. Spent so far is suppressed (no source independent of the projection call).
  if (!hasPlan) {
    return (
      <section
        className={`${card} p-6 md:p-8`}
        data-testid="on-track-tile"
        aria-label="No plan for this period"
      >
        <header className="mb-6 flex items-center justify-between">
          <span className="text-xs font-semibold uppercase tracking-[0.08em] text-text-muted">
            Plan vs Projection
          </span>
          <span className="text-xs text-text-secondary">This period</span>
        </header>
        <div className="grid grid-cols-1 gap-6 md:grid-cols-2 lg:grid-cols-4">
          <Stat label="PLAN" value={formatAmount(0)} sublabel="not yet planned" muted />
          <Stat label="SPENT SO FAR" value="—" muted />
          <Stat label="VARIANCE" value="—" muted />
          <Stat label="PROJECTED" value="—" muted />
        </div>
        <div className="mt-6 text-sm">
          <Link
            href="/forecast-plans"
            className="text-text-primary underline underline-offset-2 hover:text-text-secondary"
          >
            No plan for this period. Set one up →
          </Link>
        </div>
      </section>
    );
  }

  // Plan exists, projection call failed. Plan stays; Spent / Variance / Projected suppress.
  if (projectionFailed) {
    return (
      <section
        className={`${card} p-6 md:p-8`}
        data-testid="on-track-tile"
        aria-label="Projection unavailable"
      >
        <header className="mb-6 flex items-center justify-between">
          <span className="text-xs font-semibold uppercase tracking-[0.08em] text-text-muted">
            Plan vs Projection
          </span>
          <span className="text-xs text-text-secondary">This period</span>
        </header>
        <div className="grid grid-cols-1 gap-6 md:grid-cols-2 lg:grid-cols-4">
          <Stat label="PLAN" value={formatAmount(plannedExpense)} sublabel="full month" />
          <Stat label="SPENT SO FAR" value="—" muted />
          <Stat label="VARIANCE" value="—" muted />
          <Stat label="PROJECTED" value="Unavailable" muted />
        </div>
        <div className="mt-6 flex flex-wrap items-center gap-3 text-sm text-text-muted">
          <span>Projection unavailable.</span>
          <button
            type="button"
            onClick={onRetryProjection}
            disabled={projectionLoading}
            className={`${btnSecondary} text-xs disabled:opacity-50`}
          >
            <RefreshCw className="mr-1 inline h-3.5 w-3.5" aria-hidden="true" />
            Retry
          </button>
        </div>
      </section>
    );
  }

  // Plan exists but projection hasn't loaded yet. Show plan with placeholders for the rest.
  if (!projection) {
    return (
      <section
        className={`${card} p-6 md:p-8`}
        data-testid="on-track-tile"
        aria-label="Loading projection"
      >
        <header className="mb-6 flex items-center justify-between">
          <span className="text-xs font-semibold uppercase tracking-[0.08em] text-text-muted">
            Plan vs Projection
          </span>
          <span className="text-xs text-text-secondary">This period</span>
        </header>
        <div className="grid grid-cols-1 gap-6 md:grid-cols-2 lg:grid-cols-4">
          <Stat label="PLAN" value={formatAmount(plannedExpense)} sublabel="full month" />
          <Stat label="SPENT SO FAR" value="…" muted />
          <Stat label="VARIANCE" value="…" muted />
          <Stat label="PROJECTED" value="…" muted />
        </div>
      </section>
    );
  }

  const executedExpense = Number(projection.executed_expense);
  const forecastExpense = Number(projection.forecast_expense);

  // Past period: verdict + variance use actuals (executed_expense), not the projection.
  // Projected column is suppressed; Final spent replaces it.
  if (isPastPeriod) {
    const pct = executedExpense / plannedExpense;
    const verdict = computeVerdict(pct);
    const variance = plannedExpense - executedExpense;
    const varianceFavorable = variance >= 0;

    return (
      <section className={`${card} p-6 md:p-8`} data-testid="on-track-tile" aria-label={PAST_LABELS[verdict]}>
        <header className="mb-6 flex items-center justify-between gap-2">
          <h2
            className={`flex items-center gap-2 text-2xl font-semibold uppercase tabular-nums md:text-3xl ${VERDICT_COLOR[verdict]}`}
          >
            <VerdictIcon verdict={verdict} />
            <span>{PAST_LABELS[verdict]}</span>
          </h2>
          <span className="text-xs text-text-secondary">Past period</span>
        </header>
        <div className="grid grid-cols-1 gap-6 md:grid-cols-3">
          <Stat label="PLAN" value={formatAmount(plannedExpense)} sublabel="full month" />
          <Stat
            label="FINAL SPENT"
            value={formatAmount(executedExpense)}
            sublabel="final"
          />
          <Stat
            label="VARIANCE"
            value={`${variance >= 0 ? "+" : "−"}${formatAmount(Math.abs(variance))}`}
            sublabel={varianceFavorable ? "under plan" : "over plan"}
            valueClass={varianceFavorable ? "text-accent" : "text-danger"}
          />
        </div>
      </section>
    );
  }

  // Default current-period view: verdict + variance use the projection.
  const pct = forecastExpense / plannedExpense;
  const verdict = computeVerdict(pct);
  const variance = plannedExpense - forecastExpense;
  const varianceFavorable = variance >= 0;

  return (
    <section className={`${card} p-6 md:p-8`} data-testid="on-track-tile" aria-label={CURRENT_LABELS[verdict]}>
      <header className="mb-6 flex items-center justify-between gap-2">
        <h2
          className={`flex items-center gap-2 text-2xl font-semibold uppercase tabular-nums md:text-3xl ${VERDICT_COLOR[verdict]}`}
        >
          <VerdictIcon verdict={verdict} />
          <span>{CURRENT_LABELS[verdict]}</span>
        </h2>
        <span className="text-xs text-text-secondary">This period</span>
      </header>
      <div className="grid grid-cols-1 gap-6 md:grid-cols-2 lg:grid-cols-4">
        <Stat label="PLAN" value={formatAmount(plannedExpense)} sublabel="full month" />
        <Stat
          label="SPENT SO FAR"
          value={formatAmount(executedExpense)}
          sublabel="actual today"
        />
        <Stat
          label="VARIANCE"
          value={`${variance >= 0 ? "+" : "−"}${formatAmount(Math.abs(variance))}`}
          sublabel={varianceFavorable ? "under plan" : "over plan"}
          valueClass={varianceFavorable ? "text-accent" : "text-danger"}
        />
        <Stat
          label="PROJECTED"
          value={formatAmount(forecastExpense)}
          sublabel="forecast for month"
        />
      </div>
    </section>
  );
}
