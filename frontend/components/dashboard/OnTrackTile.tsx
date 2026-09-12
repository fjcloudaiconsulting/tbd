"use client";

import Link from "next/link";
import { AlertCircle, AlertTriangle, Check, RefreshCw } from "lucide-react";
import { btnSecondary, card } from "@/lib/styles";

import { formatMoney } from "@/lib/format";
import { useMoney } from "@/lib/hooks/use-org-currency";

export interface ForecastPlanLike {
  total_planned_expense: string | number;
}

export interface CurrencyScopeLike {
  currency: string | null;
  excluded_currencies: string[];
  excluded_account_count: number;
}

export interface ForecastProjectionLike {
  executed_expense: string | number;
  forecast_expense: string | number;
  /**
   * TBD-325 PR 2. Which currency the figures above are denominated in, and
   * what was left out to get them. Arrives IN-BAND on the projection, so both
   * dashboards receive it for free: each spreads the whole projection object
   * through to this tile.
   */
  currency_scope?: CurrencyScopeLike;
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

// Verdict bands: <=95% on track, 95-105% watch, >105% over.
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
      <p className="text-xs font-semibold uppercase tracking-[0.08em] text-text-muted">
        {label}
      </p>
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

function DetailsLink() {
  return (
    <div className="mt-6 text-sm">
      <Link
        href="/forecast-plans"
        className="text-text-primary underline underline-offset-2 hover:text-text-secondary"
      >
        View forecast details
      </Link>
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
  const money = useMoney();
  const plannedExpense = forecastPlan ? Number(forecastPlan.total_planned_expense) : 0;
  const hasPlan = forecastPlan !== null && plannedExpense > 0;

  // Past period with no plan: non-actionable, past-tense copy. Runs
  // BEFORE the no-plan branch so a closed period without a plan doesn't
  // render the current-period CTA.
  if (isPastPeriod && !hasPlan) {
    return (
      <section
        className={`${card} p-4 md:p-6`}
        data-testid="on-track-tile"
        aria-label="No plan was set for this period"
      >
        <header className="mb-4 flex items-center justify-between">
          <span className="text-xs font-semibold uppercase tracking-[0.08em] text-text-muted">
            Forecast
          </span>
          <span className="text-xs text-text-secondary">Past period</span>
        </header>
        <p className="text-sm text-text-muted">No plan was set for this period.</p>
      </section>
    );
  }

  // Future period: prompt to plan ahead.
  if (isFuturePeriod) {
    return (
      <section className={`${card} p-4 md:p-6`} data-testid="on-track-tile" aria-label="Plan ahead">
        <header className="mb-4 flex items-center justify-between">
          <span className="text-xs font-semibold uppercase tracking-[0.08em] text-text-muted">
            Forecast
          </span>
          <span className="text-xs text-text-secondary">Future period</span>
        </header>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Stat
            label="Planned spending"
            value={hasPlan ? money(plannedExpense) : "—"}
            sublabel={hasPlan ? "full month" : "not yet planned"}
            muted={!hasPlan}
          />
          <Stat label="Spent so far" value="—" sublabel="nothing yet" muted />
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

  // Current period, no plan exists.
  if (!hasPlan) {
    return (
      <section
        className={`${card} p-4 md:p-6`}
        data-testid="on-track-tile"
        aria-label="No plan for this period"
      >
        <header className="mb-4 flex items-center justify-between">
          <span className="text-xs font-semibold uppercase tracking-[0.08em] text-text-muted">
            Forecast
          </span>
          <span className="text-xs text-text-secondary">This period</span>
        </header>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Stat label="Planned spending" value={money(0)} sublabel="not yet planned" muted />
          <Stat label="Spent so far" value="—" muted />
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

  // Plan exists, projection call failed.
  if (projectionFailed) {
    return (
      <section
        className={`${card} p-4 md:p-6`}
        data-testid="on-track-tile"
        aria-label="Projection unavailable"
      >
        <header className="mb-4 flex items-center justify-between">
          <span className="text-xs font-semibold uppercase tracking-[0.08em] text-text-muted">
            Forecast
          </span>
          <span className="text-xs text-text-secondary">This period</span>
        </header>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Stat label="Planned spending" value={money(plannedExpense)} sublabel="full month" />
          <Stat label="Spent so far" value="—" muted />
        </div>
        <div className="mt-6 flex flex-wrap items-center gap-3 text-sm text-text-muted">
          <span>Forecast unavailable.</span>
          <button
            type="button"
            onClick={onRetryProjection}
            disabled={projectionLoading}
            // Distinguishing name: the Spending donut and the dashboard's
            // refresh banner each render their own "Retry" alongside this one.
            aria-label="Retry loading the forecast"
            className={`${btnSecondary} text-xs disabled:opacity-50`}
          >
            <RefreshCw className="mr-1 inline h-3.5 w-3.5" aria-hidden="true" />
            Retry
          </button>
        </div>
      </section>
    );
  }

  // Plan exists but projection hasn't loaded yet.
  if (!projection) {
    return (
      <section
        className={`${card} p-4 md:p-6`}
        data-testid="on-track-tile"
        aria-label="Loading projection"
      >
        <header className="mb-4 flex items-center justify-between">
          <span className="text-xs font-semibold uppercase tracking-[0.08em] text-text-muted">
            Forecast
          </span>
          <span className="text-xs text-text-secondary">This period</span>
        </header>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Stat label="Planned spending" value={money(plannedExpense)} sublabel="full month" />
          <Stat label="Spent so far" value="…" muted />
        </div>
      </section>
    );
  }

  const executedExpense = Number(projection.executed_expense);
  const forecastExpense = Number(projection.forecast_expense);

  // ── TBD-325 PR 2: money was excluded, so there is no verdict to give ──
  //
  // ⚠ ONE branch, placed ABOVE the past/current fork deliberately. Both ratio
  // sites (`isPastPeriod` below, and the current-period one after it) are
  // downstream of here, so both are covered by this single guard. Duplicating
  // it into each branch is how the two drift apart.
  //
  // ⚠ KEYED ON THE SCOPE, NEVER ON AN AMOUNT. `executedExpense === 0` is a
  // LEGITIMATE value -- a fully-pending month -- and rendering it as ON TRACK
  // is a previously reported bug that `on-track-tile.test.tsx` fences. Guarding
  // on zero re-opens it. Only `excluded_account_count` distinguishes "the scope
  // deleted your spending" from "nothing has settled yet"; no arithmetic on the
  // numerator can.
  //
  // ⚠ SAFE BY MEASUREMENT, NOT BY CONSTRUCTION. As of 2026-09-12 no API path
  // produces `excluded_account_count > 0`: account rows are built at exactly
  // two sites, both guarded, currency is immutable post-create, and legacy
  // multi-currency orgs backfill to `primary_currency = NULL` (which scopes
  // nothing). Measured over 13 scenarios and 24 race trials. This branch is
  // the backstop for the next unguarded insert site -- PR 1 shipped with one
  // of two doors open and nothing noticed for a month.
  //
  // The FIGURES are kept: they are correct for the scoped currency, merely
  // incomplete. It is the VERDICT that would be a lie, so only the verdict
  // goes.
  const scope = projection.currency_scope;
  if (scope && scope.excluded_account_count > 0) {
    const excluded = scope.excluded_currencies.join(", ");
    const n = scope.excluded_account_count;
    // ⚠ `scoped`, not `money`. `useMoney` resolves through
    // `deriveOrgCurrency(accounts)`, which returns undefined for a
    // multi-currency org -- correctly, because at org level there is no single
    // honest answer. But INSIDE THIS BRANCH there is: every figure here was
    // computed under `scope.currency`, the server said so in band, and the
    // sentence below names it. Rendering them bare would leave the tile saying
    // "Covers your EUR accounts only" directly beneath three unlabelled
    // numbers, which is the one place the symbol is both knowable and load
    // bearing. The org-level degradation is untouched everywhere else.
    const scoped = (v: number) =>
      scope.currency ? formatMoney(v, scope.currency) : money(v);
    return (
      <section
        className={`${card} p-4 md:p-6`}
        data-testid="on-track-tile"
        aria-label="Forecast, partial currency scope"
      >
        <header className="mb-4 flex items-center justify-between gap-2">
          <span className="text-xs font-semibold uppercase tracking-[0.08em] text-text-muted">
            Forecast
          </span>
          <span className="text-xs text-text-secondary">
            {isPastPeriod ? "Past period" : "This period"}
          </span>
        </header>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          <Stat
            label="Planned spending"
            value={scoped(plannedExpense)}
            sublabel="full month"
            muted
          />
          <Stat
            label={isPastPeriod ? "Final spent" : "Spent so far"}
            value={scoped(executedExpense)}
            sublabel={isPastPeriod ? "final" : "actual today"}
          />
          {!isPastPeriod && (
            <Stat
              label="Expected spending"
              value={scoped(forecastExpense)}
              sublabel="end of month"
              muted
            />
          )}
        </div>
        <div
          className="mt-3 text-xs text-text-secondary"
          data-testid="on-track-currency-scope"
          role="status"
        >
          Covers your {scope.currency} accounts only. {n}{" "}
          {n === 1 ? "account" : "accounts"} in {excluded}{" "}
          {n === 1 ? "is" : "are"} not included, because totals across
          currencies would be meaningless.
        </div>
        <DetailsLink />
      </section>
    );
  }

  // Past period: verdict uses actuals (executed_expense), not the projection.
  if (isPastPeriod) {
    const pct = executedExpense / plannedExpense;
    const verdict = computeVerdict(pct);

    return (
      <section className={`${card} p-4 md:p-6`} data-testid="on-track-tile" aria-label={PAST_LABELS[verdict]}>
        <header className="mb-4 flex items-center justify-between gap-2">
          <h2
            className={`flex items-center gap-2 text-2xl font-semibold uppercase tabular-nums md:text-3xl ${VERDICT_COLOR[verdict]}`}
          >
            <VerdictIcon verdict={verdict} />
            <span>{PAST_LABELS[verdict]}</span>
          </h2>
          <span className="text-xs text-text-secondary">Past period</span>
        </header>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Stat
            label="Planned spending"
            value={money(plannedExpense)}
            sublabel="full month"
            muted
          />
          <Stat label="Final spent" value={money(executedExpense)} sublabel="final" />
        </div>
        <DetailsLink />
      </section>
    );
  }

  // Current period: verdict anchors on actual (settled) spending. Expected
  // spending is shown as a supporting fact, not the verdict driver. YNAB /
  // Monarch / Copilot / Mint all behave this way.
  const pct = executedExpense / plannedExpense;
  const verdict = computeVerdict(pct);

  return (
    <section className={`${card} p-4 md:p-6`} data-testid="on-track-tile" aria-label={CURRENT_LABELS[verdict]}>
      <header className="mb-4 flex items-center justify-between gap-2">
        <h2
          className={`flex items-center gap-2 text-2xl font-semibold uppercase tabular-nums md:text-3xl ${VERDICT_COLOR[verdict]}`}
        >
          <VerdictIcon verdict={verdict} />
          <span>{CURRENT_LABELS[verdict]}</span>
        </h2>
        <span className="text-xs text-text-secondary">This period</span>
      </header>
      {/* Metric hierarchy: Spent so far is the primary visual anchor
          (the daily-glance number); Planned and Expected are muted
          secondary baselines/projections. */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <Stat
          label="Planned spending"
          value={money(plannedExpense)}
          sublabel="full month"
          muted
        />
        <Stat
          label="Spent so far"
          value={money(executedExpense)}
          sublabel="actual today"
        />
        <Stat
          label="Expected spending"
          value={money(forecastExpense)}
          sublabel="end of month"
          muted
        />
      </div>
      <DetailsLink />
    </section>
  );
}
