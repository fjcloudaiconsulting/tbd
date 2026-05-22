"use client";

/**
 * Plans comparison page — side-by-side projections (PR3 of the
 * Plans train).
 *
 * Architect-locked invariants:
 * - Read-only view. No params edits here — the editor lives at
 *   /plans (the inline panel) and "Open" on each verdict row
 *   navigates back to the editor.
 * - Hard cap of 3 plans. The picker disables additional checks
 *   beyond the third selection.
 * - Single horizon applies to every plan in the compare. The
 *   default is the smallest of the selected plans' horizons (so
 *   trip + retirement defaults to 120, not 480 — keeps the
 *   visual range sane).
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";

import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";
import {
  ComparisonView,
  type CompareProjection,
} from "@/components/scenarios/ComparisonView";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import {
  btnPrimary,
  btnSecondary,
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
  input,
  label as labelCls,
  pageTitle,
} from "@/lib/styles";

interface PlanRow {
  id: number;
  name: string;
  scenario_type: "trip" | "purchase" | "retirement" | "custom";
  horizon_months: number;
}

const MAX_COMPARE = 3;

const HORIZON_CAPS = {
  trip: 120,
  purchase: 120,
  custom: 120,
  retirement: 480,
} as const;

function minCapAcross(selectedTypes: Array<keyof typeof HORIZON_CAPS>): number {
  if (selectedTypes.length === 0) return 24;
  let cap: number = HORIZON_CAPS.retirement;
  for (const t of selectedTypes) {
    if (HORIZON_CAPS[t] < cap) cap = HORIZON_CAPS[t];
  }
  return cap;
}

export default function ComparePlansPage() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const [plans, setPlans] = useState<PlanRow[]>([]);
  const [selected, setSelected] = useState<number[]>([]);
  const [horizon, setHorizon] = useState<number>(24);
  const [results, setResults] = useState<CompareProjection[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    if (loading) return;
    if (!user) {
      router.replace("/login");
    }
  }, [loading, user, router]);

  const loadPlans = useCallback(async () => {
    try {
      const rows = await apiFetch<PlanRow[]>("/api/v1/scenarios");
      setPlans(rows);
    } catch (e) {
      setErr(extractErrorMessage(e, "Failed to load plans"));
    }
  }, []);

  useEffect(() => {
    if (user) void loadPlans();
  }, [user, loadPlans]);

  const selectedTypes = useMemo(() => {
    const types = selected
      .map((id) => plans.find((p) => p.id === id)?.scenario_type)
      .filter((t): t is keyof typeof HORIZON_CAPS => Boolean(t));
    return types;
  }, [selected, plans]);

  const horizonCap = useMemo(
    () => minCapAcross(selectedTypes),
    [selectedTypes],
  );

  function toggle(id: number) {
    setSelected((cur) => {
      if (cur.includes(id)) return cur.filter((x) => x !== id);
      if (cur.length >= MAX_COMPARE) return cur;
      return [...cur, id];
    });
  }

  async function runCompare() {
    setBusy(true);
    setErr("");
    try {
      const body = {
        scenario_ids: selected,
        horizon_months: horizon,
      };
      const data = await apiFetch<{ projections: CompareProjection[] }>(
        "/api/v1/scenarios/compare",
        { method: "POST", body: JSON.stringify(body) },
      );
      setResults(data.projections);
    } catch (e) {
      setErr(extractErrorMessage(e, "Compare failed"));
    } finally {
      setBusy(false);
    }
  }

  if (loading || !user) return null;

  return (
    <AppShell>
      <header className="mb-4 flex items-center justify-between">
        <div>
          <h1 className={`${pageTitle} mb-0`}>Compare plans</h1>
          <p className="mt-1 text-sm text-text-muted">
            Pick up to three plans to see their projected balances on the
            same chart. Read-only.
          </p>
        </div>
        <button
          type="button"
          className={`${btnSecondary} sm:min-h-0`}
          onClick={() => router.push("/plans")}
          data-testid="compare-back"
        >
          Back to plans
        </button>
      </header>

      {err && <p className={`mb-3 ${errorCls}`}>{err}</p>}

      <section className={`${card} mb-4 p-5`} data-testid="compare-picker">
        <header className={`mb-3 ${cardHeader}`}>
          <h2 className={cardTitle}>Pick plans (1 to 3)</h2>
        </header>
        {plans.length === 0 ? (
          <p className="text-sm text-text-muted">No plans yet. Create one first.</p>
        ) : (
          <ul className="space-y-2">
            {plans.map((plan) => {
              const checked = selected.includes(plan.id);
              const disabled = !checked && selected.length >= MAX_COMPARE;
              return (
                <li key={plan.id} className="flex items-center gap-3">
                  <input
                    id={`compare-plan-${plan.id}`}
                    type="checkbox"
                    checked={checked}
                    disabled={disabled}
                    onChange={() => toggle(plan.id)}
                    data-testid={`compare-plan-checkbox-${plan.id}`}
                  />
                  <label
                    htmlFor={`compare-plan-${plan.id}`}
                    className="text-sm text-text-primary"
                  >
                    {plan.name}
                    <span className="ml-2 text-xs text-text-muted">
                      {plan.scenario_type}
                    </span>
                  </label>
                </li>
              );
            })}
          </ul>
        )}

        <div className="mt-4 flex items-end gap-3">
          <div>
            <label className={labelCls} htmlFor="compare-horizon">
              Horizon (months, max {horizonCap})
            </label>
            <input
              id="compare-horizon"
              type="number"
              min={1}
              max={horizonCap}
              value={horizon}
              onChange={(e) => setHorizon(Number(e.target.value || 0))}
              className={input}
              data-testid="compare-horizon-input"
            />
          </div>
          <button
            type="button"
            className={`${btnPrimary} sm:min-h-0`}
            onClick={() => void runCompare()}
            disabled={busy || selected.length === 0 || horizon < 1}
            data-testid="compare-run"
          >
            Compare
          </button>
        </div>
      </section>

      {results.length > 0 && (
        <section className={`${card} p-5`} data-testid="compare-results">
          <header className={`mb-3 ${cardHeader}`}>
            <h2 className={cardTitle}>Projection</h2>
          </header>
          <ComparisonView
            projections={results}
            onOpen={(scenarioId) => router.push(`/plans?open=${scenarioId}`)}
          />
        </section>
      )}
    </AppShell>
  );
}
