"use client";

/**
 * Plans page — life-event simulation sandbox (spec 2026-05-22).
 *
 * Architect-locked invariants:
 * - UI says "Plans" everywhere. The word "scenario" never appears
 *   in user-visible copy (internal code uses ``scenarios``).
 * - Per-user visibility: this page only shows the current user's
 *   plans. The backend enforces it; the UI never asks for an org
 *   list. Sharing is out of scope for v1.
 * - Re-simulate on params change is debounced ~400ms so a fast
 *   typist doesn't pummel the backend.
 * - Trip and Purchase templates are fully wired in PR1.
 *   Retirement + Custom render a "Available in a later release"
 *   surface (params stub + simulate still works, but the editor
 *   is minimal).
 * - The user-visible "Apply this plan as real transactions" button
 *   is OUT OF SCOPE for v1 per the architect lock.
 */

import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Loader2 } from "lucide-react";

import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";
import { CustomParamsEditor } from "@/components/scenarios/CustomParamsEditor";
import { ProjectionChart } from "@/components/scenarios/ProjectionChart";
import { RetirementParamsEditor } from "@/components/scenarios/RetirementParamsEditor";
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

// Editor snapshot — the "committed" state Save/Discard pivot around.
// PR #plans-editor-save-discard adds explicit Save and Discard buttons
// on top of the existing debounced auto-PATCH behaviour. The snapshot
// captures the state the editor "considers committed"; Save advances
// the snapshot to the current local state, Discard rolls local state
// (and the server row) back to the snapshot. Auto-PATCH keeps firing
// for live projection feedback — the buttons are a UX layer on top.
type EditorSnapshot = {
  name: string;
  horizon: number;
  params: Record<string, unknown>;
};

// Internal type name uses the DB / API word; the UI label is "Plans".
export type ScenarioType = "trip" | "purchase" | "retirement" | "custom";

export interface Account {
  id: number;
  name: string;
  currency: string;
  balance: string;
}

export interface ProjectionPoint {
  month: string;
  projected_balance: string;
}

export interface AccountSeries {
  account_id: number;
  account_name: string;
  currency: string;
  points: ProjectionPoint[];
}

export interface DipAlert {
  account_id: number;
  month: string;
  projected_balance: string;
  trigger: string;
  severity: "info" | "warn" | "critical";
}

export interface AffordabilityVerdict {
  color: "green" | "yellow" | "red";
  headline: string;
  reason: string;
}

export interface Suggestion {
  action: string;
  expected_outcome: string;
  by_days?: number | null;
  by_amount?: string | null;
}

export interface RealTermsSeries {
  points: ProjectionPoint[];
  inflation_pct: string;
}

export interface ProjectionResult {
  engine_name: string;
  computed_at: string;
  horizon_months: number;
  currency: string;
  per_account_series: AccountSeries[];
  alerts: DipAlert[];
  verdict: AffordabilityVerdict;
  suggestions: Suggestion[];
  real_terms_series?: RealTermsSeries | null;
  smoothed_with_regression?: boolean;
}

export interface Scenario {
  id: number;
  org_id: number;
  user_id: number;
  name: string;
  scenario_type: ScenarioType;
  params_json: Record<string, unknown>;
  projection_json: ProjectionResult | null;
  projection_engine: string | null;
  projection_computed_at: string | null;
  horizon_months: number;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

const TYPE_LABEL: Record<ScenarioType, string> = {
  trip: "Trip",
  purchase: "Purchase",
  retirement: "Retirement",
  custom: "Custom",
};

const VERDICT_BADGE: Record<AffordabilityVerdict["color"], string> = {
  green: "bg-success-dim text-success",
  yellow: "bg-accent/15 text-accent",
  red: "bg-danger-dim text-danger",
};

export default function PlansPage() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const searchParams = useSearchParams();
  const [items, setItems] = useState<Scenario[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [active, setActive] = useState<Scenario | null>(null);
  const [loadErr, setLoadErr] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [createType, setCreateType] = useState<ScenarioType>("trip");
  const [itemsLoaded, setItemsLoaded] = useState(false);
  // We consume ?open=<id> exactly once per page mount so a deep-link from
  // the compare page lands directly in the editor. After we either match
  // (and open the plan) or fail to match, we clear the query string so a
  // refresh doesn't re-trigger and re-open repeatedly.
  const openParamConsumedRef = useRef(false);
  // Per-plan in-flight tracking so multiple list-row Simulate clicks
  // (across different plans) can run in parallel, while a second click
  // on the same plan is blocked. A single boolean would falsely lock
  // every row when any one is in flight.
  const [simulating, setSimulating] = useState<Set<number>>(() => new Set());
  // Per-plan "just simulated at <ts>" microcopy. Each entry clears
  // ~3s after a simulate resolves so a fresh result is visibly
  // acknowledged even if the verdict color didn't change.
  const [lastSimulatedAt, setLastSimulatedAt] = useState<Record<number, string>>({});
  const flashTimersRef = useRef<Record<number, ReturnType<typeof setTimeout>>>({});

  useEffect(() => {
    const timers = flashTimersRef.current;
    return () => {
      for (const t of Object.values(timers)) clearTimeout(t);
    };
  }, []);

  useEffect(() => {
    if (loading) return;
    if (!user) {
      router.replace("/login");
    }
  }, [loading, user, router]);

  const loadAll = useCallback(async () => {
    try {
      const [rows, accs] = await Promise.all([
        apiFetch<Scenario[]>("/api/v1/scenarios"),
        apiFetch<Account[]>("/api/v1/accounts"),
      ]);
      setItems(rows);
      setAccounts(accs);
    } catch (e) {
      setLoadErr(extractErrorMessage(e, "Failed to load plans"));
    } finally {
      setItemsLoaded(true);
    }
  }, []);

  useEffect(() => {
    if (user) void loadAll();
  }, [user, loadAll]);

  // ?open=<id> from the compare page → open the matching plan in the
  // editor as soon as the scenarios list has loaded. If no scenario
  // matches, just drop the param and stay on the list. Either way,
  // strip the query string so a refresh doesn't re-open.
  useEffect(() => {
    if (openParamConsumedRef.current) return;
    if (!user) return;
    if (!itemsLoaded) return;
    const raw = searchParams?.get("open");
    if (!raw) return;
    const id = Number(raw);
    if (Number.isFinite(id)) {
      const match = items.find((s) => s.id === id);
      if (match) setActive(match);
    }
    openParamConsumedRef.current = true;
    router.replace("/plans");
  }, [searchParams, items, itemsLoaded, user, router]);

  async function deletePlan(id: number) {
    try {
      await apiFetch(`/api/v1/scenarios/${id}`, { method: "DELETE" });
      setItems((rows) => rows.filter((r) => r.id !== id));
      if (active && active.id === id) setActive(null);
    } catch (e) {
      setLoadErr(extractErrorMessage(e, "Delete failed"));
    }
  }

  async function simulate(plan: Scenario): Promise<Scenario | null> {
    setSimulating((prev) => {
      const next = new Set(prev);
      next.add(plan.id);
      return next;
    });
    try {
      const next = await apiFetch<Scenario>(
        `/api/v1/scenarios/${plan.id}/simulate`,
        { method: "POST", body: JSON.stringify({ engine: "analytic", options: {} }) },
      );
      setItems((rows) => rows.map((r) => (r.id === next.id ? next : r)));
      if (active && active.id === next.id) setActive(next);
      // Flash an "Updated," timestamp next to the verdict pill so the
      // user has visible feedback even when the verdict color is
      // identical to the previous run. The flash clears after ~3s.
      const stamp = new Date().toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });
      setLastSimulatedAt((prev) => ({ ...prev, [plan.id]: stamp }));
      if (flashTimersRef.current[plan.id]) {
        clearTimeout(flashTimersRef.current[plan.id]);
      }
      flashTimersRef.current[plan.id] = setTimeout(() => {
        setLastSimulatedAt((prev) => {
          const { [plan.id]: _drop, ...rest } = prev;
          return rest;
        });
        delete flashTimersRef.current[plan.id];
      }, 3000);
      return next;
    } catch (e) {
      setLoadErr(extractErrorMessage(e, "Simulate failed"));
      return null;
    } finally {
      setSimulating((prev) => {
        const next = new Set(prev);
        next.delete(plan.id);
        return next;
      });
    }
  }

  if (loading || !user) {
    return null;
  }

  return (
    <AppShell>
      <header className="mb-4 flex items-center justify-between gap-3">
        <div>
          <h1 className={`${pageTitle} mb-0`}>Plans</h1>
          <p className="mt-1 text-sm text-text-muted">
            Plan one-off life events. Nothing here touches your real transactions.{" "}
            <Link
              href="/docs/plans"
              className="underline-offset-2 hover:underline"
              data-testid="plans-docs-link"
            >
              Read the Plans guide.
            </Link>
          </p>
        </div>
        <div className="flex items-center gap-2">
          {items.length >= 2 && (
            <button
              type="button"
              className={`${btnSecondary} sm:min-h-0`}
              onClick={() => router.push("/plans/compare")}
              data-testid="plans-compare"
            >
              Compare plans
            </button>
          )}
          <button
            type="button"
            className={`${btnPrimary} sm:min-h-0`}
            onClick={() => setCreateOpen(true)}
            data-testid="plans-new"
          >
            + New plan
          </button>
        </div>
      </header>

      {loadErr && <p className={`mb-3 ${errorCls}`}>{loadErr}</p>}

      {active ? (
        <PlanEditor
          // Key by plan.id so React unmounts + remounts the editor when
          // the active plan changes. This naturally resets all local
          // state (editorValid, params draft, etc.) so a previous
          // plan's invalid editor state can't leak into the next plan
          // and silently suppress its debounced PATCHes.
          key={active.id}
          plan={active}
          accounts={accounts}
          onBack={() => setActive(null)}
          onSimulate={simulate}
          onUpdated={(updated) => {
            setItems((rows) => rows.map((r) => (r.id === updated.id ? updated : r)));
            setActive(updated);
          }}
        />
      ) : (
        <PlansList
          items={items}
          onOpen={setActive}
          onDelete={deletePlan}
          onSimulate={simulate}
          isSimulating={(id) => simulating.has(id)}
          lastSimulatedAt={lastSimulatedAt}
        />
      )}

      {createOpen && (
        <NewPlanModal
          type={createType}
          accounts={accounts}
          onTypeChange={setCreateType}
          onClose={() => setCreateOpen(false)}
          onCreated={(plan) => {
            setItems((rows) => [plan, ...rows]);
            setActive(plan);
            setCreateOpen(false);
          }}
        />
      )}
    </AppShell>
  );
}


// ── List view ───────────────────────────────────────────────────────────


function PlansList({
  items,
  onOpen,
  onDelete,
  onSimulate,
  isSimulating,
  lastSimulatedAt,
}: {
  items: Scenario[];
  onOpen: (plan: Scenario) => void;
  onDelete: (id: number) => void;
  onSimulate: (plan: Scenario) => Promise<Scenario | null>;
  isSimulating: (planId: number) => boolean;
  lastSimulatedAt: Record<number, string>;
}) {
  if (items.length === 0) {
    return (
      <section className={`${card} p-6`} data-testid="plans-empty">
        <p className="text-sm text-text-muted">
          No plans yet. Use the New plan button to sketch a trip, purchase, or
          life event and see how it plays out month by month.
        </p>
      </section>
    );
  }

  return (
    <section className={`${card} p-0`} data-testid="plans-list">
      <ul className="divide-y divide-border">
        {items.map((plan) => {
          const busy = isSimulating(plan.id);
          const flashStamp = lastSimulatedAt[plan.id];
          return (
            <li
              key={plan.id}
              className="flex items-center justify-between gap-4 px-5 py-3"
            >
              <button
                type="button"
                className="flex-1 text-left"
                onClick={() => onOpen(plan)}
                data-testid={`plan-row-${plan.id}`}
              >
                <p className="text-sm font-medium text-text-primary">{plan.name}</p>
                <p className="text-xs text-text-muted">
                  {TYPE_LABEL[plan.scenario_type]} · Horizon {plan.horizon_months}mo
                </p>
              </button>
              <div
                className="flex items-center gap-2"
                role="status"
                aria-live="polite"
                data-testid={`plan-verdict-region-${plan.id}`}
              >
                {plan.projection_json && (
                  <span
                    className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${
                      VERDICT_BADGE[plan.projection_json.verdict.color]
                    }`}
                  >
                    {plan.projection_json.verdict.color.toUpperCase()}
                  </span>
                )}
                {flashStamp && (
                  <span
                    className="text-[11px] text-text-muted"
                    data-testid={`plan-simulate-flash-${plan.id}`}
                  >
                    Updated, {flashStamp}
                  </span>
                )}
              </div>
              <button
                type="button"
                className={`${btnSecondary} sm:min-h-0 inline-flex items-center justify-center gap-1.5`}
                onClick={() => void onSimulate(plan)}
                disabled={busy}
                aria-busy={busy}
                data-testid={`plan-simulate-${plan.id}`}
              >
                {busy && (
                  <Loader2
                    className="h-3.5 w-3.5 animate-spin motion-reduce:animate-none"
                    aria-hidden="true"
                    data-testid={`plan-simulate-spinner-${plan.id}`}
                  />
                )}
                {busy ? "Simulating..." : "Simulate"}
              </button>
              <button
                type="button"
                className="text-xs text-danger underline-offset-2 hover:underline"
                onClick={() => onDelete(plan.id)}
                data-testid={`plan-delete-${plan.id}`}
              >
                Delete
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}


// ── Editor (params left + projection right) ─────────────────────────────


function PlanEditor({
  plan,
  accounts,
  onBack,
  onSimulate,
  onUpdated,
}: {
  plan: Scenario;
  accounts: Account[];
  onBack: () => void;
  onSimulate: (plan: Scenario) => Promise<Scenario | null>;
  onUpdated: (plan: Scenario) => void;
}) {
  const [params, setParams] = useState<Record<string, unknown>>(plan.params_json);
  const [name, setName] = useState(plan.name);
  const [horizon, setHorizon] = useState<number>(plan.horizon_months);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  // Client-side validity flag set by child editors (RetirementParamsEditor
  // owns curve validation). When any inline error is showing, the
  // debounced PATCH below skips the network call so we don't spam the
  // server with 422s while the user is still typing. The inline error
  // is already visible to the user; firing the request on top of that
  // is pure noise.
  const [editorValid, setEditorValid] = useState(true);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Save/Discard snapshot. We use state (not useRef) because isDirty
  // is computed against the snapshot DURING render, and the new
  // react-hooks/refs lint rule forbids reading refs in the render
  // phase. Storing the snapshot in state keeps it observable as part
  // of the render contract while still letting Save/Discard advance
  // or restore it imperatively.
  //
  // The initializer captures the editor's starting baseline ONCE per
  // mount. The snapshot then advances only via handleSave (after the
  // server PATCH acks the new value) or handleDiscard (after the
  // server PATCH acks the rollback). Crucially there is no effect
  // tying the snapshot to the `plan` prop: the parent feeds every
  // auto-PATCH response back through `onUpdated -> setItems ->
  // active`, which would re-fire such an effect and silently advance
  // the snapshot to the just-PATCHed value, collapsing dirty state
  // and destroying Discard's rollback target. The PlanEditor is
  // keyed by `plan.id` upstream, so switching plans remounts the
  // component and re-runs this initializer with the new plan's
  // baseline — that's the only path that resets the snapshot.
  const [snapshot, setSnapshot] = useState<EditorSnapshot>(() => ({
    name: plan.name,
    horizon: plan.horizon_months,
    params: plan.params_json,
  }));
  // Microcopy line announced via aria-live so screen readers and a
  // glanceable visual indicator both see "Saved" / "Discarded" after
  // the corresponding action.
  const [statusMsg, setStatusMsg] = useState<string>("");

  // Horizon bounds match the server-side validator. Pre-checking on the
  // client keeps the debounced PATCH from firing 422s while the user
  // walks an out-of-range number up or down with the spinner.
  const horizonMax = plan.scenario_type === "retirement" ? 480 : 120;
  const horizonValid = horizon >= 1 && horizon <= horizonMax;
  const nameValid = name.trim().length > 0;
  const isValid = editorValid && horizonValid && nameValid;

  // isDirty compares current editor state to the snapshot (NOT to
  // `plan`). The debounced auto-PATCH keeps the server in sync with
  // every keystroke; the snapshot moves only when the user clicks
  // Save or Discard. Switching plans remounts the editor (via
  // key={plan.id} upstream), which re-runs the snapshot initializer
  // against the new plan's baseline. That's why "Save" is meaningful
  // even though every change already round-tripped to the server:
  // Save advances the pivot Discard reverts to.
  const isDirty =
    name !== snapshot.name ||
    horizon !== snapshot.horizon ||
    JSON.stringify(params) !== JSON.stringify(snapshot.params);

  // Stale-status guard: once the user makes a fresh change after a
  // "Saved" / "Discarded" microcopy, the message is no longer accurate.
  // Clear it as soon as the editor goes dirty again.
  useEffect(() => {
    if (isDirty && statusMsg) {
      setStatusMsg("");
    }
  }, [isDirty, statusMsg]);

  const persist = useCallback(async () => {
    setBusy(true);
    setErr("");
    try {
      const updated = await apiFetch<Scenario>(
        `/api/v1/scenarios/${plan.id}`,
        {
          method: "PATCH",
          body: JSON.stringify({
            name,
            horizon_months: horizon,
            params: {
              scenario_type: plan.scenario_type,
              ...params,
            },
          }),
        },
      );
      onUpdated(updated);
      // Re-simulate immediately after a successful PATCH so the chart
      // reflects the new params.
      await onSimulate(updated);
    } catch (e) {
      setErr(extractErrorMessage(e, "Update failed"));
    } finally {
      setBusy(false);
    }
  }, [plan.id, plan.scenario_type, name, horizon, params, onUpdated, onSimulate]);

  // Debounced re-simulate when the editor's local params drift from the
  // server-side plan. Architect-locked 400ms.
  //
  // Gate on isValid so a typed-but-incomplete curve row (or any other
  // inline validation error) doesn't fire a PATCH that the server will
  // reject with 422. The inline error is already on screen; the
  // network noise on top would just spam the console. Once the user
  // fixes the offending field, isValid flips back to true and the next
  // change-driven render schedules a fresh debounce.
  useEffect(() => {
    if (
      JSON.stringify(params) === JSON.stringify(plan.params_json)
      && name === plan.name
      && horizon === plan.horizon_months
    ) {
      return;
    }
    if (!isValid) {
      if (debounceRef.current) clearTimeout(debounceRef.current);
      return;
    }
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      void persist();
    }, 400);
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [params, name, horizon, plan, persist, isValid]);

  async function manualSimulate() {
    setBusy(true);
    setErr("");
    try {
      await onSimulate(plan);
    } finally {
      setBusy(false);
    }
  }

  // Save persists the current local state to the server and only then
  // advances the snapshot. The auto-PATCH layer keeps the chart in
  // sync between edits, but it's *gated* on isValid — when the editor
  // is invalid (e.g., a retirement curve row missing `from`), the
  // auto-PATCH intentionally short-circuits. Without the explicit
  // server round-trip here, clicking Save in an invalid state would
  // advance the snapshot to a value the server never accepted, and
  // Discard would happily roll forward to that broken state. So Save
  // is also gated on isValid (button disabled below), and we still
  // PATCH explicitly so the snapshot only advances on the server-
  // acknowledged value.
  async function handleSave() {
    if (!isDirty || !isValid) return;
    // Cancel any pending debounced PATCH so the explicit Save isn't
    // racing the auto-PATCH for the same row. Content is identical
    // either way; killing the debounce just keeps the call count
    // tight and avoids ordering surprises in tests.
    if (debounceRef.current) {
      clearTimeout(debounceRef.current);
      debounceRef.current = null;
    }
    setBusy(true);
    setErr("");
    try {
      const updated = await apiFetch<Scenario>(
        `/api/v1/scenarios/${plan.id}`,
        {
          method: "PATCH",
          body: JSON.stringify({
            name,
            horizon_months: horizon,
            params: {
              scenario_type: plan.scenario_type,
              ...params,
            },
          }),
        },
      );
      // Advance the snapshot to the server-acknowledged value. If the
      // PATCH rejected (422 / 500 / network blip), the catch branch
      // leaves the snapshot at its previous good state, so Discard
      // can still bail the user out.
      const serverParams =
        (updated.params_json as Record<string, unknown>) ?? params;
      setSnapshot({
        name: updated.name,
        horizon: updated.horizon_months,
        params: serverParams,
      });
      onUpdated(updated);
      await onSimulate(updated);
      setStatusMsg("Saved");
    } catch (e) {
      setStatusMsg("");
      setErr(`Save failed: ${extractErrorMessage(e, "unknown error")}`);
    } finally {
      setBusy(false);
    }
  }

  // Discard rolls local state back to the snapshot AND PATCHes the
  // server row with the snapshot values. The server PATCH is required
  // because the auto-PATCH layer has already written every interim
  // edit; Discard is the only way to undo those server-side writes.
  async function handleDiscard() {
    if (!isDirty) return;
    const target = snapshot;
    setBusy(true);
    setErr("");
    try {
      const updated = await apiFetch<Scenario>(
        `/api/v1/scenarios/${plan.id}`,
        {
          method: "PATCH",
          body: JSON.stringify({
            name: target.name,
            horizon_months: target.horizon,
            params: {
              scenario_type: plan.scenario_type,
              ...target.params,
            },
          }),
        },
      );
      // Reset local editor state in lockstep with the rolled-back
      // server row. Don't await onUpdated — its `setActive` would loop
      // through the snapshot-resetting effect we already control here.
      setParams(target.params);
      setName(target.name);
      setHorizon(target.horizon);
      onUpdated(updated);
      await onSimulate(updated);
      setStatusMsg("Discarded");
    } catch (e) {
      setErr(extractErrorMessage(e, "Discard failed"));
    } finally {
      setBusy(false);
    }
  }

  const projection = plan.projection_json;

  return (
    <section data-testid="plan-editor">
      <button
        type="button"
        className="mb-3 text-xs text-text-muted underline-offset-2 hover:underline"
        onClick={onBack}
      >
        ← Back to plans
      </button>
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
        <div className={`${card} p-5`}>
          <header className={`mb-3 ${cardHeader}`}>
            <h2 className={cardTitle}>Params</h2>
          </header>
          {err && <p className={`mb-2 ${errorCls}`}>{err}</p>}
          <div className="space-y-3">
            <div>
              <label className={labelCls} htmlFor="plan-name">Name</label>
              <input
                id="plan-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                className={input}
                data-testid="plan-name-input"
              />
            </div>
            <div>
              <label className={labelCls} htmlFor="plan-horizon">Horizon (months)</label>
              <input
                id="plan-horizon"
                type="number"
                min={1}
                max={horizonMax}
                value={horizon}
                onChange={(e) => setHorizon(Number(e.target.value || 0))}
                className={input}
                aria-describedby="plan-horizon-hint"
              />
              <p
                id="plan-horizon-hint"
                className="mt-1 max-w-prose text-xs text-text-muted"
              >
                How many months to project from today. Max{" "}
                {plan.scenario_type === "retirement"
                  ? "480 (40 years) for retirement"
                  : "120 (10 years) for trip and purchase plans"}.
              </p>
            </div>
            {plan.scenario_type === "trip" && (
              <TripParamsEditor params={params} setParams={setParams} accounts={accounts} />
            )}
            {plan.scenario_type === "purchase" && (
              <PurchaseParamsEditor params={params} setParams={setParams} accounts={accounts} />
            )}
            {plan.scenario_type === "retirement" && (
              <RetirementParamsEditor
                params={params}
                setParams={setParams}
                accounts={accounts}
                onValidityChange={setEditorValid}
              />
            )}
            {plan.scenario_type === "custom" && (
              <CustomParamsEditor
                params={params}
                setParams={setParams}
                accounts={accounts}
              />
            )}
            {/* Save / Discard / Re-simulate cluster.
                isDirty drives both Save and Discard so an unchanged
                editor doesn't offer either control. The "Unsaved
                changes" hint is the visual mirror of isDirty for the
                user; the aria-live region beneath it announces the
                resulting state ("Saved" / "Discarded") for assistive
                tech and as a glanceable confirmation. */}
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                className={`${btnPrimary} sm:min-h-0`}
                onClick={handleSave}
                disabled={!isDirty || busy || !isValid}
                data-testid="plan-save"
              >
                Save
              </button>
              <button
                type="button"
                className={`${btnSecondary} sm:min-h-0`}
                onClick={handleDiscard}
                disabled={!isDirty || busy}
                data-testid="plan-discard"
              >
                Discard
              </button>
              <button
                type="button"
                className={`${btnSecondary} sm:min-h-0`}
                onClick={manualSimulate}
                disabled={busy}
                data-testid="plan-simulate-now"
              >
                Re-simulate
              </button>
              {isDirty && isValid && (
                <span
                  className="text-xs text-text-muted"
                  data-testid="plan-dirty-indicator"
                >
                  Unsaved changes
                </span>
              )}
              {isDirty && !isValid && (
                <span
                  className="text-xs text-text-muted"
                  data-testid="plan-invalid-hint"
                >
                  Fix validation errors before saving.
                </span>
              )}
            </div>
            <p
              role="status"
              aria-live="polite"
              className="min-h-[1rem] text-xs text-text-muted"
              data-testid="plan-save-status"
            >
              {statusMsg}
            </p>
          </div>
        </div>
        <div className={`${card} p-5`}>
          <header className={`mb-3 ${cardHeader}`}>
            <h2 className={cardTitle}>Projection</h2>
          </header>
          {projection ? (
            <ProjectionView projection={projection} />
          ) : (
            <p className="text-sm text-text-muted">
              No projection yet. Click Re-simulate to compute one.
            </p>
          )}
        </div>
      </div>
    </section>
  );
}


function ProjectionView({ projection }: { projection: ProjectionResult }) {
  return (
    <div data-testid="projection-view">
      <div className="mb-3 flex items-center gap-2">
        <span
          className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${VERDICT_BADGE[projection.verdict.color]}`}
          data-testid="verdict-badge"
        >
          {projection.verdict.color.toUpperCase()}
        </span>
        <span className="text-sm text-text-primary">{projection.verdict.headline}</span>
        {projection.smoothed_with_regression && (
          <span
            className="ml-auto rounded-full bg-info-dim px-2 py-0.5 text-[11px] text-info"
            data-testid="projection-smoothed-badge"
          >
            Trend-adjusted
          </span>
        )}
      </div>
      <p className="mb-4 text-xs text-text-muted">{projection.verdict.reason}</p>
      <ProjectionChart projection={projection} />
      {projection.alerts.length > 0 && (
        <div className="mt-4">
          <p className="mb-1 text-xs uppercase tracking-wide text-text-muted">Alerts</p>
          <ul className="space-y-1">
            {projection.alerts.map((a, idx) => (
              <li key={`${a.account_id}-${a.month}-${idx}`} className={`text-xs ${errorCls}`}>
                {a.month}: dip to {a.projected_balance} ({a.trigger})
              </li>
            ))}
          </ul>
        </div>
      )}
      {projection.suggestions.length > 0 && (
        <div className="mt-4" data-testid="projection-suggestions">
          <p className="mb-1 text-xs uppercase tracking-wide text-text-muted">Suggestions</p>
          <ul className="space-y-1">
            {projection.suggestions.map((s, idx) => (
              <li key={idx} className="text-xs text-text-primary">
                {s.expected_outcome}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}


// Shared helper-text class. `max-w-prose` keeps the explanation
// readable on wide layouts and prevents a long sentence from forcing
// the right pane into horizontal scroll on narrow viewports.
const fieldHelp = "mt-1 max-w-prose text-xs text-text-muted";

function TripParamsEditor({
  params,
  setParams,
  accounts,
}: {
  params: Record<string, unknown>;
  setParams: (next: Record<string, unknown>) => void;
  accounts: Account[];
}) {
  function set(key: string, value: unknown) {
    setParams({ ...params, [key]: value });
  }
  return (
    <>
      <div>
        <label className={labelCls} htmlFor="trip-destination">Destination</label>
        <input
          id="trip-destination"
          value={(params.destination as string) ?? ""}
          onChange={(e) => set("destination", e.target.value)}
          className={input}
          data-testid="trip-destination-input"
          aria-describedby="trip-destination-hint"
        />
        <p id="trip-destination-hint" className={fieldHelp}>
          Free text. Used only as a label on the plan; the projection ignores it.
        </p>
      </div>
      <div>
        <label className={labelCls} htmlFor="trip-start">Start date</label>
        <input
          id="trip-start"
          type="date"
          value={(params.start_date as string) ?? ""}
          onChange={(e) => set("start_date", e.target.value)}
          className={input}
          aria-describedby="trip-start-hint"
        />
        <p id="trip-start-hint" className={fieldHelp}>
          When the trip kicks off. The cost lands in a single dip on this month.
        </p>
      </div>
      <div>
        <label className={labelCls} htmlFor="trip-duration">Duration (days)</label>
        <input
          id="trip-duration"
          type="number"
          min={1}
          max={365}
          value={(params.duration_days as number) ?? 1}
          onChange={(e) => set("duration_days", Number(e.target.value || 0))}
          className={input}
          aria-describedby="trip-duration-hint"
        />
        <p id="trip-duration-hint" className={fieldHelp}>
          Multiplied by daily budget and accommodation per night to get the on-the-ground total.
        </p>
      </div>
      <div>
        <label className={labelCls} htmlFor="trip-transport">Transport cost</label>
        <input
          id="trip-transport"
          type="number"
          step="0.01"
          min="0"
          value={(params.transport_cost as string) ?? "0"}
          onChange={(e) => set("transport_cost", e.target.value)}
          className={input}
          aria-describedby="trip-transport-hint"
        />
        <p id="trip-transport-hint" className={fieldHelp}>
          Flights, trains, fuel. Counted once at the start of the trip.
        </p>
      </div>
      <div>
        <label className={labelCls} htmlFor="trip-accom">Accommodation per night</label>
        <input
          id="trip-accom"
          type="number"
          step="0.01"
          min="0"
          value={(params.accommodation_per_night as string) ?? "0"}
          onChange={(e) => set("accommodation_per_night", e.target.value)}
          className={input}
          aria-describedby="trip-accom-hint"
        />
        <p id="trip-accom-hint" className={fieldHelp}>
          Multiplied by duration to get the total stay cost.
        </p>
      </div>
      <div>
        <label className={labelCls} htmlFor="trip-daily">Daily budget</label>
        <input
          id="trip-daily"
          type="number"
          step="0.01"
          min="0"
          value={(params.daily_budget as string) ?? "0"}
          onChange={(e) => set("daily_budget", e.target.value)}
          className={input}
          aria-describedby="trip-daily-hint"
        />
        <p id="trip-daily-hint" className={fieldHelp}>
          Food, sights, ground transport. Multiplied by duration.
        </p>
      </div>
      <div>
        <label className={labelCls} htmlFor="trip-account">Source account</label>
        <select
          id="trip-account"
          value={(params.source_account_id as number) ?? ""}
          onChange={(e) => set("source_account_id", Number(e.target.value))}
          className={input}
          aria-describedby="trip-account-hint"
        >
          {accounts.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name} ({a.currency})
            </option>
          ))}
        </select>
        <p id="trip-account-hint" className={fieldHelp}>
          Which account the trip's cash dip is deducted from in the projection.
        </p>
      </div>
    </>
  );
}


function PurchaseParamsEditor({
  params,
  setParams,
  accounts,
}: {
  params: Record<string, unknown>;
  setParams: (next: Record<string, unknown>) => void;
  accounts: Account[];
}) {
  function set(key: string, value: unknown) {
    setParams({ ...params, [key]: value });
  }
  return (
    <>
      <div>
        <label className={labelCls} htmlFor="p-subtype">Subtype</label>
        <input
          id="p-subtype"
          value={(params.subtype as string) ?? "car"}
          onChange={(e) => set("subtype", e.target.value)}
          className={input}
          aria-describedby="p-subtype-hint"
        />
        <p id="p-subtype-hint" className={fieldHelp}>
          Free text (car, house, appliance, etc.). Used only as a label.
        </p>
      </div>
      <div>
        <label className={labelCls} htmlFor="p-label">Label</label>
        <input
          id="p-label"
          value={(params.label as string) ?? ""}
          onChange={(e) => set("label", e.target.value)}
          className={input}
          aria-describedby="p-label-hint"
        />
        <p id="p-label-hint" className={fieldHelp}>
          Short name shown on the plan, for example "Family car 2027".
        </p>
      </div>
      <div>
        <label className={labelCls} htmlFor="p-target">Target date</label>
        <input
          id="p-target"
          type="date"
          value={(params.target_date as string) ?? ""}
          onChange={(e) => set("target_date", e.target.value)}
          className={input}
          aria-describedby="p-target-hint"
        />
        <p id="p-target-hint" className={fieldHelp}>
          When the purchase happens. The down payment lands on this month in the projection.
        </p>
      </div>
      <div>
        <label className={labelCls} htmlFor="p-price">Total price</label>
        <input
          id="p-price"
          type="number"
          step="0.01"
          min="0"
          value={(params.total_price as string) ?? "0"}
          onChange={(e) => set("total_price", e.target.value)}
          className={input}
          aria-describedby="p-price-hint"
        />
        <p id="p-price-hint" className={fieldHelp}>
          Sticker price before any financing. Reference number only; the projection moves cash based on down payment and (later) monthly financing.
        </p>
      </div>
      <div>
        <label className={labelCls} htmlFor="p-down">Down payment</label>
        <input
          id="p-down"
          type="number"
          step="0.01"
          min="0"
          value={(params.down_payment as string) ?? "0"}
          onChange={(e) => set("down_payment", e.target.value)}
          className={input}
          aria-describedby="p-down-hint"
        />
        <p id="p-down-hint" className={fieldHelp}>
          Cash you put up front on the target date. Comes out of the account you pick below.
        </p>
      </div>
      <div>
        <label className={labelCls} htmlFor="p-account">Down-payment account</label>
        <select
          id="p-account"
          value={(params.down_payment_account_id as number) ?? ""}
          onChange={(e) => set("down_payment_account_id", Number(e.target.value))}
          className={input}
          aria-describedby="p-account-hint"
        >
          {accounts.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name} ({a.currency})
            </option>
          ))}
        </select>
        <p id="p-account-hint" className={fieldHelp}>
          Which account funds the down payment in the projection.
        </p>
      </div>
    </>
  );
}


// ── New plan modal ──────────────────────────────────────────────────────


function NewPlanModal({
  type,
  accounts,
  onTypeChange,
  onClose,
  onCreated,
}: {
  type: ScenarioType;
  accounts: Account[];
  onTypeChange: (t: ScenarioType) => void;
  onClose: () => void;
  onCreated: (plan: Scenario) => void;
}) {
  const [name, setName] = useState("New plan");
  const [destination, setDestination] = useState("Lisbon, Portugal");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      const firstAccount = accounts[0]?.id;
      if (!firstAccount) {
        throw new Error(
          "Create at least one account before creating a plan."
        );
      }
      const baseParams: Record<string, unknown> = {
        scenario_type: type,
      };
      if (type === "trip") {
        Object.assign(baseParams, {
          destination,
          start_date: new Date().toISOString().slice(0, 10),
          duration_days: 7,
          currency: accounts[0]?.currency ?? "EUR",
          transport_cost: "0",
          accommodation_per_night: "0",
          daily_budget: "0",
          one_off_extras: [],
          source_account_id: firstAccount,
        });
      } else if (type === "purchase") {
        Object.assign(baseParams, {
          subtype: "car",
          label: name,
          target_date: new Date().toISOString().slice(0, 10),
          currency: accounts[0]?.currency ?? "EUR",
          total_price: "0",
          down_payment: "0",
          down_payment_account_id: firstAccount,
          financing: null,
        });
      } else if (type === "retirement") {
        Object.assign(baseParams, {
          target_retirement_date: new Date().toISOString().slice(0, 10),
          currency: accounts[0]?.currency ?? "EUR",
          monthly_contribution: "500.00",
          contribution_account_id: firstAccount,
          target_balance: "100000.00",
          annual_return_pct: "6.0",
          inflation_pct: "2.5",
          contribution_curve: [],
        });
      } else {
        Object.assign(baseParams, {
          label: name,
          events: [],
        });
      }
      const plan = await apiFetch<Scenario>("/api/v1/scenarios", {
        method: "POST",
        body: JSON.stringify({
          name,
          scenario_type: type,
          horizon_months: type === "retirement" ? 240 : 24,
          params: baseParams,
        }),
      });
      onCreated(plan);
    } catch (e) {
      setErr(extractErrorMessage(e, "Create failed"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      role="dialog"
      aria-label="New plan"
      className="fixed inset-0 z-50 flex items-center justify-center bg-bg/70 p-4"
      data-testid="new-plan-modal"
    >
      <form
        onSubmit={submit}
        className={`${card} w-full max-w-md p-5`}
      >
        <header className={`mb-3 ${cardHeader}`}>
          <h2 className={cardTitle}>New plan</h2>
        </header>
        {err && <p className={`mb-2 ${errorCls}`}>{err}</p>}
        <div className="mb-3">
          <label className={labelCls} htmlFor="np-template">Template</label>
          <select
            id="np-template"
            value={type}
            onChange={(e) => onTypeChange(e.target.value as ScenarioType)}
            className={input}
            data-testid="new-plan-template"
          >
            <option value="trip">Trip</option>
            <option value="purchase">Purchase</option>
            <option value="retirement">Retirement</option>
            <option value="custom">Custom</option>
          </select>
        </div>
        <div className="mb-3">
          <label className={labelCls} htmlFor="np-name">Name</label>
          <input
            id="np-name"
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            className={input}
            data-testid="new-plan-name"
          />
        </div>
        {type === "trip" && (
          <div className="mb-3">
            <label className={labelCls} htmlFor="np-dest">Destination</label>
            <input
              id="np-dest"
              required
              value={destination}
              onChange={(e) => setDestination(e.target.value)}
              className={input}
              data-testid="new-plan-destination"
            />
          </div>
        )}
        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            className={`${btnSecondary} sm:min-h-0`}
            onClick={onClose}
          >
            Cancel
          </button>
          <button
            type="submit"
            className={`${btnPrimary} sm:min-h-0`}
            disabled={busy}
            data-testid="new-plan-submit"
          >
            Create
          </button>
        </div>
      </form>
    </div>
  );
}
