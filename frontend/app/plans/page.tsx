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
import { useRouter } from "next/navigation";

import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";
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

export interface ProjectionResult {
  engine_name: string;
  computed_at: string;
  horizon_months: number;
  currency: string;
  per_account_series: AccountSeries[];
  alerts: DipAlert[];
  verdict: AffordabilityVerdict;
  suggestions: Suggestion[];
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
  const [items, setItems] = useState<Scenario[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [active, setActive] = useState<Scenario | null>(null);
  const [loadErr, setLoadErr] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [createType, setCreateType] = useState<ScenarioType>("trip");

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
    }
  }, []);

  useEffect(() => {
    if (user) void loadAll();
  }, [user, loadAll]);

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
    try {
      const next = await apiFetch<Scenario>(
        `/api/v1/scenarios/${plan.id}/simulate`,
        { method: "POST", body: JSON.stringify({ engine: "analytic", options: {} }) },
      );
      setItems((rows) => rows.map((r) => (r.id === next.id ? next : r)));
      if (active && active.id === next.id) setActive(next);
      return next;
    } catch (e) {
      setLoadErr(extractErrorMessage(e, "Simulate failed"));
      return null;
    }
  }

  if (loading || !user) {
    return null;
  }

  return (
    <AppShell>
      <header className="mb-4 flex items-center justify-between">
        <div>
          <h1 className={`${pageTitle} mb-0`}>Plans</h1>
          <p className="mt-1 text-sm text-text-muted">
            Plan one-off life events. Nothing here touches your real transactions.
          </p>
        </div>
        <button
          type="button"
          className={`${btnPrimary} sm:min-h-0`}
          onClick={() => setCreateOpen(true)}
          data-testid="plans-new"
        >
          + New plan
        </button>
      </header>

      {loadErr && <p className={`mb-3 ${errorCls}`}>{loadErr}</p>}

      {active ? (
        <PlanEditor
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
}: {
  items: Scenario[];
  onOpen: (plan: Scenario) => void;
  onDelete: (id: number) => void;
  onSimulate: (plan: Scenario) => Promise<Scenario | null>;
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
        {items.map((plan) => (
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
            {plan.projection_json && (
              <span
                className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${
                  VERDICT_BADGE[plan.projection_json.verdict.color]
                }`}
              >
                {plan.projection_json.verdict.color.toUpperCase()}
              </span>
            )}
            <button
              type="button"
              className={`${btnSecondary} sm:min-h-0`}
              onClick={() => void onSimulate(plan)}
              data-testid={`plan-simulate-${plan.id}`}
            >
              Simulate
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
        ))}
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
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Reset state when switching plans.
  useEffect(() => {
    setParams(plan.params_json);
    setName(plan.name);
    setHorizon(plan.horizon_months);
  }, [plan.id, plan.params_json, plan.name, plan.horizon_months]);

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
  useEffect(() => {
    if (
      JSON.stringify(params) === JSON.stringify(plan.params_json)
      && name === plan.name
      && horizon === plan.horizon_months
    ) {
      return;
    }
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      void persist();
    }, 400);
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [params, name, horizon, plan, persist]);

  async function manualSimulate() {
    setBusy(true);
    setErr("");
    try {
      await onSimulate(plan);
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
                max={plan.scenario_type === "retirement" ? 480 : 120}
                value={horizon}
                onChange={(e) => setHorizon(Number(e.target.value || 0))}
                className={input}
              />
            </div>
            {plan.scenario_type === "trip" && (
              <TripParamsEditor params={params} setParams={setParams} accounts={accounts} />
            )}
            {plan.scenario_type === "purchase" && (
              <PurchaseParamsEditor params={params} setParams={setParams} accounts={accounts} />
            )}
            {(plan.scenario_type === "retirement" || plan.scenario_type === "custom") && (
              <p className="text-xs text-text-muted">
                The {TYPE_LABEL[plan.scenario_type].toLowerCase()} template editor is
                available in a later release.
              </p>
            )}
            <button
              type="button"
              className={`${btnPrimary} sm:min-h-0`}
              onClick={manualSimulate}
              disabled={busy}
              data-testid="plan-simulate-now"
            >
              Re-simulate
            </button>
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
      <div className={`mb-3 flex items-center gap-2`}>
        <span
          className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${VERDICT_BADGE[projection.verdict.color]}`}
          data-testid="verdict-badge"
        >
          {projection.verdict.color.toUpperCase()}
        </span>
        <span className="text-sm text-text-primary">{projection.verdict.headline}</span>
      </div>
      <p className="mb-4 text-xs text-text-muted">{projection.verdict.reason}</p>
      <ul className="mb-4 space-y-1">
        {projection.per_account_series.map((s) => {
          const last = s.points[s.points.length - 1];
          return (
            <li key={s.account_id} className="text-sm text-text-primary">
              <span className="font-medium">{s.account_name}</span>{" "}
              <span className="text-text-muted">
                ends at {last?.projected_balance ?? "?"} {s.currency}
              </span>
            </li>
          );
        })}
      </ul>
      {projection.alerts.length > 0 && (
        <div className="mb-4">
          <p className={`mb-1 text-xs uppercase tracking-wide text-text-muted`}>Alerts</p>
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
        <div>
          <p className={`mb-1 text-xs uppercase tracking-wide text-text-muted`}>Suggestions</p>
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
        />
      </div>
      <div>
        <label className={labelCls} htmlFor="trip-start">Start date</label>
        <input
          id="trip-start"
          type="date"
          value={(params.start_date as string) ?? ""}
          onChange={(e) => set("start_date", e.target.value)}
          className={input}
        />
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
        />
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
        />
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
        />
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
        />
      </div>
      <div>
        <label className={labelCls} htmlFor="trip-account">Source account</label>
        <select
          id="trip-account"
          value={(params.source_account_id as number) ?? ""}
          onChange={(e) => set("source_account_id", Number(e.target.value))}
          className={input}
        >
          {accounts.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name} ({a.currency})
            </option>
          ))}
        </select>
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
        />
      </div>
      <div>
        <label className={labelCls} htmlFor="p-label">Label</label>
        <input
          id="p-label"
          value={(params.label as string) ?? ""}
          onChange={(e) => set("label", e.target.value)}
          className={input}
        />
      </div>
      <div>
        <label className={labelCls} htmlFor="p-target">Target date</label>
        <input
          id="p-target"
          type="date"
          value={(params.target_date as string) ?? ""}
          onChange={(e) => set("target_date", e.target.value)}
          className={input}
        />
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
        />
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
        />
      </div>
      <div>
        <label className={labelCls} htmlFor="p-account">Down-payment account</label>
        <select
          id="p-account"
          value={(params.down_payment_account_id as number) ?? ""}
          onChange={(e) => set("down_payment_account_id", Number(e.target.value))}
          className={input}
        >
          {accounts.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name} ({a.currency})
            </option>
          ))}
        </select>
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
          monthly_contribution: "0",
          contribution_account_id: firstAccount,
          target_balance: "0",
          annual_return_pct: "5.0",
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
