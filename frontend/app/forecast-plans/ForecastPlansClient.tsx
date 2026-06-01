"use client";

import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import useSWR from "swr";
import AppShell from "@/components/AppShell";
import HelpAnchor from "@/components/HelpAnchor";
import HelpTooltip from "@/components/Tooltip";
import Spinner from "@/components/ui/Spinner";
import ConfirmModal from "@/components/ui/ConfirmModal";
import CategorySelect from "@/components/ui/CategorySelect";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";
import { isAdmin } from "@/lib/auth";
import { formatAmount } from "@/lib/format";
import {
  input,
  label,
  btnPrimary,
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
  pageTitle,
  btnLink,
  btnDanger,
} from "@/lib/styles";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  Cell,

} from "recharts";
import type { BillingPeriod, Category, ForecastPlan, ForecastPlanItem } from "@/lib/types";
import { chartColor } from "@/lib/chart-colors";
import { useTransactionAddedListener } from "@/lib/hooks/use-transaction-added";

// "Auto" is the honest label for source=history (PR #146 #1). populate
// surfaces both 3-month-average rows AND current-period-only rows under
// the same flag, so calling the bucket "Avg (3mo)" was a lie when a one-off
// furniture purchase showed up. Matches the L3.10 import preview "Auto"
// badge convention.
const SOURCE_LABELS: Record<string, string> = {
  manual: "Manual",
  recurring: "Recurring",
  history: "Auto",
};

// Inline help copy. The /docs#forecasts section carries the long explanation;
// these are 1-2 sentence reminders reachable from the controls themselves.
const HELP_VARIANCE =
  "Actual minus planned, per category. Negative means you spent less than planned for an expense category (good); positive means you spent more (over budget). Income variance flips: positive is good, negative is short of plan.";
const HELP_AUTO_POPULATE =
  "Fill the empty plan from your recurring bills, your last 3 months of activity, and what's already booked this period. Use this when you first build the plan.";
const HELP_REFRESH =
  "Re-run the auto-fill against today's templates and history. Drops Recurring and Auto rows, keeps anything you typed by hand. Drafts only.";
const HELP_EDIT_PLAN =
  "Plans go through Draft (editable) and Finalized (read-only, actuals tracked live). Edit Plan reverts the finalized plan to draft so you can change numbers or refresh.";
const DOCS_HINT = " See /docs#forecasts for more.";

// Tiny `(?)` indicator with native title tooltip. No design-system tooltip
// primitive exists yet; native title is honest and ships now.
function HelpIcon({ label, text }: { label: string; text: string }) {
  return (
    <span
      tabIndex={0}
      role="img"
      aria-label={`${label} explained: ${text}`}
      title={text + DOCS_HINT}
      className="ml-1 cursor-help rounded-sm text-text-muted/70 hover:text-text-secondary focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
    >
      (?)
    </span>
  );
}

type Props = {
  initialPeriods: BillingPeriod[];
  initialCategories: Category[];
  initialPlan: ForecastPlan | null;
};

// A master and the rows that build its forecast. For a master-level item
// the group is a single self-named row (subItems empty). For subcategory
// items the group rolls them up: planned/actual/variance are the master
// totals and `subItems` carries the individual sub rows.
type MasterGroup = {
  masterId: number;
  masterName: string;
  planned: number;
  actual: number;
  variance: number;
  subItems: ForecastPlanItem[];
  isMasterLevel: boolean;
};

export default function ForecastPlansClient({
  initialPeriods,
  initialCategories,
  initialPlan,
}: Props) {
  const router = useRouter();
  const { user } = useAuth();
  const admin = user ? isAdmin(user) : false;

  // Categories and periods are stable per session — seed once from the
  // server, then evolve locally as the user creates categories via the
  // inline CategorySelect picker.
  const [categories, setCategories] = useState<Category[]>(initialCategories);
  const [periods, setPeriods] = useState<BillingPeriod[]>(initialPeriods);

  // Default to the current period (open = no end_date), not index 0.
  // Mirrors the pre-RSC loadRefs() logic.
  const initialPeriodIdx = (() => {
    const idx = initialPeriods.findIndex((p) => p.end_date === null);
    return idx >= 0 ? idx : 0;
  })();
  const [periodIdx, setPeriodIdx] = useState(initialPeriodIdx);

  const selectedPeriod = periods.length > 0 ? periods[periodIdx] : null;
  const periodStart = selectedPeriod?.start_date ?? "";

  const [error, setError] = useState("");
  // Non-blocking refresh-error state for the AppShell post-write event
  // listener. The page keeps the previous plan; banner offers a Retry.
  const [refreshError, setRefreshError] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [showForm, setShowForm] = useState(false);

  // Add form
  const [formCategoryId, setFormCategoryId] = useState<number | "">("");
  const [formType, setFormType] = useState<"income" | "expense">("expense");
  const [formAmount, setFormAmount] = useState("");

  // Edit
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editAmount, setEditAmount] = useState("");

  // View filter
  const [viewFilter, setViewFilter] = useState<"all" | "income" | "expense">(
    "all"
  );

  // Confirm modal
  const [confirmAction, setConfirmAction] = useState<{
    title: string;
    message: string;
    variant: "warning" | "danger";
    confirmLabel?: string;
    action: () => void;
  } | null>(null);

  // Show details toggle. Default off; persisted under
  // forecast-plans:show-details. Initial render reads localStorage in a
  // mount effect so server/client HTML stays identical (no hydration
  // mismatch). Until the effect runs, treat the toggle as "off" — the
  // simpler view — so flipping after hydration only ever reveals more.
  const [showDetails, setShowDetails] = useState<boolean>(false);
  const [showDetailsHydrated, setShowDetailsHydrated] = useState<boolean>(false);

  useEffect(() => {
    try {
      const raw = localStorage.getItem("forecast-plans:show-details");
      if (raw === "true") setShowDetails(true);
    } catch {
      // localStorage unavailable (private mode etc.) — keep default off.
    }
    setShowDetailsHydrated(true);
  }, []);

  useEffect(() => {
    if (!showDetailsHydrated) return;
    try {
      localStorage.setItem(
        "forecast-plans:show-details",
        showDetails ? "true" : "false",
      );
    } catch {
      // ignore
    }
  }, [showDetails, showDetailsHydrated]);

  // Plan SWR. Key is the period-scoped GET endpoint that get-or-creates a
  // draft for the visible period. `fallbackData` seeds the server-fetched
  // plan so first paint has no spinner. The mutate function lets every
  // imperative write below (populate, refresh-from-sources, add/update/
  // delete item, activate/revert/discard) push a fresh plan into the cache
  // without an extra network round-trip — we already have the response
  // from the POST.
  const planKey = periodStart
    ? `/api/v1/forecast-plans?period_start=${periodStart}`
    : null;
  const {
    data: planData,
    mutate: mutatePlan,
    isLoading: planLoading,
  } = useSWR<ForecastPlan | null>(
    planKey,
    (key: string) => apiFetch<ForecastPlan>(key),
    {
      fallbackData: initialPlan,
      // Stay on the same period's data while we re-fetch.
      keepPreviousData: true,
    },
  );
  // Guard against `keepPreviousData` leaking stale plan state during
  // period navigation: SWR holds the previous period's plan while the
  // new key resolves, but mutations dispatched against that plan would
  // hit the WRONG plan ID. Only expose `planData` to the rest of the
  // component when its `period_start` matches the currently-selected
  // period. During the gap the page shows its loading state (see the
  // `fetching` derivation below) and action handlers no-op on null.
  const plan: ForecastPlan | null =
    planData && planData.period_start === periodStart ? planData : null;

  const isActive = plan?.status === "active";
  const isDraft = plan?.status === "draft";
  const hasItems = (plan?.items?.length ?? 0) > 0;

  // Per-org forecast build granularity (master | subcategory). The plan
  // response carries the org setting so every member knows the mode without
  // an admin-only GET /settings call. Local state mirrors it and flips
  // optimistically when an admin uses the granularity control; it re-syncs
  // whenever a fresh plan lands.
  const [mode, setMode] = useState<"master" | "subcategory">(
    initialPlan?.forecast_input_granularity ?? "master",
  );
  const [savingMode, setSavingMode] = useState(false);
  useEffect(() => {
    if (plan?.forecast_input_granularity) {
      setMode(plan.forecast_input_granularity);
    }
  }, [plan?.forecast_input_granularity]);

  // Determine period context label
  const today = new Date().toISOString().slice(0, 10);
  const isFuturePeriod = selectedPeriod
    ? selectedPeriod.start_date > today
    : false;
  const isCurrentPeriod = selectedPeriod
    ? selectedPeriod.start_date <= today &&
      (!selectedPeriod.end_date || selectedPeriod.end_date >= today)
    : false;
  const isPastPeriod = selectedPeriod
    ? selectedPeriod.end_date !== null && selectedPeriod.end_date < today
    : false;

  // Refs mirroring `periods` / `periodIdx` so the long-lived
  // ensure-future async closure below can read the latest values
  // without depending on them (which would re-run the once-per-mount
  // effect every time the user navigates).
  const periodsRef = useRef(periods);
  const periodIdxRef = useRef(periodIdx);
  useEffect(() => {
    periodsRef.current = periods;
    periodIdxRef.current = periodIdx;
  }, [periods, periodIdx]);

  // ensure-future runs once per client mount. We then refresh periods
  // from the backend so any newly created stubs become navigable. The RSC
  // already seeded the initial periods, so this is purely a "catch up to
  // today" pass — quiet on failure.
  //
  // Selection-preservation: the backend lists periods newest-first, so a
  // freshly-created future stub may slot in at index 0 and silently
  // shift the user off the current period if we keep the stale
  // `periodIdx`. Find the same period (by `start_date`) in the refreshed
  // list and update the index. If their old period is gone (defensive —
  // shouldn't happen), fall back to the open period that contains today,
  // else index 0.
  const futureEnsured = useRef(false);
  useEffect(() => {
    if (futureEnsured.current) return;
    futureEnsured.current = true;
    (async () => {
      try {
        await apiFetch("/api/v1/settings/billing-periods/ensure-future", {
          method: "POST",
        });
        const fresh = await apiFetch<BillingPeriod[]>(
          "/api/v1/settings/billing-periods",
        );
        if (!Array.isArray(fresh) || fresh.length === 0) return;

        const currentStart =
          periodsRef.current[periodIdxRef.current]?.start_date;
        let nextIdx = currentStart
          ? fresh.findIndex((p) => p.start_date === currentStart)
          : -1;
        if (nextIdx === -1) {
          const today = new Date().toISOString().slice(0, 10);
          const openIdx = fresh.findIndex(
            (p) =>
              p.start_date <= today &&
              (p.end_date === null || today <= p.end_date),
          );
          nextIdx = openIdx !== -1 ? openIdx : 0;
        }
        setPeriods(fresh);
        if (nextIdx !== periodIdxRef.current) setPeriodIdx(nextIdx);
      } catch {
        // Non-fatal — the server-seeded periods are still usable.
      }
    })();
  }, []);

  // Client-side categories recovery (PR #288). Categories are seeded
  // from `initialCategories` only — there is no SWR for the categories
  // list, because they're treated as stable per session. If the RSC
  // shell rendered with an empty categories array (transient /auth/verify
  // or transient /api/v1/categories), the user would see an empty
  // category picker and be unable to add plan items. Fire a one-shot
  // recovery fetch on mount when categories are empty so the page
  // self-heals after a transient server-side fetch failure. Single
  // attempt, no retry loop — `apiFetch` has its own bounded timeout
  // (#286) and the user can always reload.
  const categoriesRecoveryAttempted = useRef(false);
  useEffect(() => {
    if (categoriesRecoveryAttempted.current) return;
    if (categories.length > 0) return;
    categoriesRecoveryAttempted.current = true;
    (async () => {
      try {
        const fresh = await apiFetch<Category[]>("/api/v1/categories");
        if (Array.isArray(fresh) && fresh.length > 0) {
          setCategories(fresh);
        }
      } catch {
        // Quiet on failure — the user can reload, and the picker still
        // exposes the inline "create new category" path that calls
        // /api/v1/categories on demand.
      }
    })();
  }, [categories.length]);

  // After a write from the AppShell-level "+ New Transaction" CTA the
  // forecast page must reload the plan so per-category actuals and
  // variance reflect the new transaction. We don't reload refs here:
  // categories and periods don't change on a transaction add. If the
  // panel created a new category, the user only sees it the next time
  // they navigate to /forecast-plans (acceptable, this page is a plan
  // editor not a category picker).
  const refreshAfterTransactionAdded = useCallback(async () => {
    if (!periodStart) return;
    setRefreshing(true);
    try {
      await mutatePlan();
      setRefreshError(false);
    } catch {
      setRefreshError(true);
    } finally {
      setRefreshing(false);
    }
  }, [periodStart, mutatePlan]);

  useTransactionAddedListener(() => {
    void refreshAfterTransactionAdded();
  });

  // Build a category-id → master-id lookup so the disable predicate can
  // reason about masters and their subs regardless of build mode.
  const masterOf = useMemo(() => {
    const m = new Map<number, number>();
    for (const c of categories) m.set(c.id, c.parent_id ?? c.id);
    return m;
  }, [categories]);

  // Disable predicate (spec R6 — replaces the old "disable the whole
  // master tree once any one is used" rule that caused the core bug).
  // For the currently-selected type:
  //   - disable the EXACT category already added (any item),
  //   - disable a master if it has subcategory items (mixing not allowed),
  //   - disable a subcategory whose master has a master-level item.
  // After adding one subcategory, OTHER subs of the same master stay
  // enabled — only the exact added one and the master are disabled.
  const disabledForType = useMemo(() => {
    const ids = new Set<number>();
    const mastersWithMasterItem = new Set<number>();
    const mastersWithSubItem = new Set<number>();
    for (const i of plan?.items ?? []) {
      if (i.type !== formType) continue;
      ids.add(i.category_id); // exact category already added
      const m = masterOf.get(i.category_id) ?? i.category_id;
      if (i.category_id === m) mastersWithMasterItem.add(m);
      else mastersWithSubItem.add(m);
    }
    for (const c of categories) {
      const isMaster = c.parent_id === null;
      if (isMaster && mastersWithSubItem.has(c.id)) ids.add(c.id);
      if (!isMaster && c.parent_id !== null && mastersWithMasterItem.has(c.parent_id)) {
        ids.add(c.id);
      }
    }
    return ids;
  }, [plan?.items, formType, categories, masterOf]);

  // Resolve the category id to store, depending on build mode.
  //   - master mode: roll a selected subcategory up to its master (legacy).
  //   - subcategory mode: store the selected category id verbatim (the
  //     backend rejects masters in this mode).
  const resolveSubmitCategoryId = (catId: number | ""): number | "" => {
    if (catId === "") return "";
    const cat = categories.find((c) => c.id === catId);
    if (!cat) return "";
    if (mode === "subcategory") return cat.id;
    return cat.parent_id ?? cat.id;
  };

  // Filtered items. Memoized on the stable upstream `plan?.items` reference
  // so downstream `chartData` (and the income/expense list renders below)
  // don't churn on every parent re-render. Without this, the `.filter()`
  // calls produced a fresh array reference every render and the
  // `useMemo([expenseItems])` for `chartData` never hit.
  const items = useMemo(
    () =>
      (plan?.items ?? []).filter(
        (i) => viewFilter === "all" || i.type === viewFilter
      ),
    [plan?.items, viewFilter],
  );
  const incomeItems = useMemo(
    () => items.filter((i) => i.type === "income"),
    [items],
  );
  const expenseItems = useMemo(
    () => items.filter((i) => i.type === "expense"),
    [items],
  );

  // Master-name lookup for grouping. Sub items carry their master id in
  // `parent_id`; resolve the display name from the categories list, falling
  // back to a generic label if the master isn't in the (session-cached)
  // list.
  const masterName = useCallback(
    (masterId: number): string => {
      const c = categories.find((cat) => cat.id === masterId);
      return c?.name ?? "Master category";
    },
    [categories],
  );

  // Group items under their master so the forecast list and chart always
  // present "the forecast is for the master" (product decision). A master's
  // total is the sum of its rows (one master-level item, OR its subs).
  // Master-level items form a single-row group named after themselves; sub
  // items group under their parent master.
  const groupByMaster = useCallback(
    (list: ForecastPlanItem[]): MasterGroup[] => {
      const byMaster = new Map<number, ForecastPlanItem[]>();
      const order: number[] = [];
      for (const it of list) {
        const m = it.parent_id ?? it.category_id;
        if (!byMaster.has(m)) {
          byMaster.set(m, []);
          order.push(m);
        }
        byMaster.get(m)!.push(it);
      }
      return order.map((m) => {
        const groupItems = byMaster.get(m)!;
        const isMasterLevel =
          groupItems.length === 1 && groupItems[0].category_id === m;
        const planned = groupItems.reduce(
          (s, i) => s + Number(i.planned_amount),
          0,
        );
        const actual = groupItems.reduce(
          (s, i) => s + Number(i.actual_amount),
          0,
        );
        return {
          masterId: m,
          masterName: isMasterLevel ? groupItems[0].category_name : masterName(m),
          planned,
          actual,
          variance: actual - planned,
          // All rows in the group. A master-level group has exactly one
          // (the master item itself); a sub group has its sub items.
          subItems: groupItems,
          isMasterLevel,
        };
      });
    },
    [masterName],
  );

  const incomeGroups = useMemo(
    () => groupByMaster(incomeItems),
    [incomeItems, groupByMaster],
  );
  const expenseGroups = useMemo(
    () => groupByMaster(expenseItems),
    [expenseItems, groupByMaster],
  );

  // Reset transient form/edit state when the visible period changes so a
  // half-completed Add/Edit on one period doesn't bleed into another.
  useEffect(() => {
    setShowForm(false);
    setEditingId(null);
  }, [periodStart]);

  // ── Actions ──────────────────────────────────────────────────────────────

  async function handlePopulate() {
    setError("");
    try {
      const p = await apiFetch<ForecastPlan>(
        `/api/v1/forecast-plans/populate?period_start=${periodStart}`,
        { method: "POST" }
      );
      await mutatePlan(p, { revalidate: false });
    } catch (err) {
      setError(extractErrorMessage(err));
    }
  }

  // Persist the org's forecast build granularity. Admin-only (the PUT
  // /settings endpoint is admin-gated; the control is hidden for members).
  // Optimistic: flip local state immediately, roll back on failure. The
  // setting is org-scoped, so all members build consistently afterward.
  async function handleSetMode(next: "master" | "subcategory") {
    if (next === mode || savingMode) return;
    const prev = mode;
    setMode(next);
    setSavingMode(true);
    setError("");
    try {
      await apiFetch("/api/v1/settings", {
        method: "PUT",
        body: JSON.stringify({
          key: "forecast_input_granularity",
          value: next,
        }),
      });
      // Re-read the plan so its forecast_input_granularity (and any
      // mode-dependent rendering) reflects the persisted setting.
      await mutatePlan();
    } catch (err) {
      setMode(prev);
      setError(extractErrorMessage(err));
    } finally {
      setSavingMode(false);
    }
  }

  async function handleRefreshFromSources() {
    setConfirmAction({
      title: "Refresh from sources",
      message:
        "This replaces auto-generated rows (recurring templates, history averages) with fresh data. Lines you added or edited yourself stay untouched.",
      variant: "warning",
      action: async () => {
        setError("");
        try {
          const p = await apiFetch<ForecastPlan>(
            `/api/v1/forecast-plans/refresh-from-sources?period_start=${periodStart}`,
            { method: "POST" }
          );
          await mutatePlan(p, { revalidate: false });
        } catch (err) {
          setError(extractErrorMessage(err));
        }
      },
    });
  }

  // Finalized-plan refresh: revert to draft, then refresh from sources.
  // The user explicitly chose to edit and refresh, so a partial failure
  // (revert ok, refresh fails) MUST leave the plan in draft and surface
  // the error — silently flipping back to active would discard the
  // user's choice without explanation.
  async function handleEditAndRefresh() {
    setConfirmAction({
      title: "Edit and refresh plan",
      message:
        "This will revert the plan to draft, replace auto-generated rows with fresh data, and keep lines you added or edited yourself.",
      variant: "warning",
      confirmLabel: "Edit and refresh",
      action: async () => {
        if (!plan) return;
        setError("");
        let revertedPlan: ForecastPlan | null = null;
        try {
          revertedPlan = await apiFetch<ForecastPlan>(
            `/api/v1/forecast-plans/${plan.id}/revert`,
            { method: "POST" },
          );
          await mutatePlan(revertedPlan, { revalidate: false });
        } catch (err) {
          setError(extractErrorMessage(err));
          return;
        }
        try {
          const refreshed = await apiFetch<ForecastPlan>(
            `/api/v1/forecast-plans/refresh-from-sources?period_start=${periodStart}`,
            { method: "POST" },
          );
          await mutatePlan(refreshed, { revalidate: false });
        } catch (err) {
          // Revert succeeded, refresh failed. Keep the draft visible
          // and surface the error so the user can retry refresh or
          // continue editing manually.
          setError(extractErrorMessage(err));
        }
      },
    });
  }

  async function handleAddItem(e: FormEvent) {
    e.preventDefault();
    if (!plan) return;
    // While the build-granularity setting is mid-save, `mode` has flipped
    // optimistically but the backend may still be in the previous mode.
    // Submitting now could send the wrong category_id (rolled-up vs verbatim)
    // and trip a confusing validation error, so block until the save settles.
    if (savingMode) {
      setError("Saving build granularity, please wait.");
      return;
    }
    setError("");
    const submitId = resolveSubmitCategoryId(formCategoryId);
    if (submitId === "") {
      setError("Please pick a category");
      return;
    }
    try {
      const p = await apiFetch<ForecastPlan>(
        `/api/v1/forecast-plans/${plan.id}/items`,
        {
          method: "POST",
          body: JSON.stringify({
            category_id: submitId,
            type: formType,
            planned_amount: formAmount,
          }),
        }
      );
      await mutatePlan(p, { revalidate: false });
      setFormCategoryId("");
      setFormAmount("");
      setShowForm(false);
    } catch (err) {
      setError(extractErrorMessage(err));
    }
  }

  async function handleUpdateItem(itemId: number) {
    if (!plan) return;
    setError("");
    try {
      const p = await apiFetch<ForecastPlan>(
        `/api/v1/forecast-plans/${plan.id}/items/${itemId}`,
        {
          method: "PUT",
          body: JSON.stringify({ planned_amount: editAmount }),
        }
      );
      await mutatePlan(p, { revalidate: false });
      setEditingId(null);
    } catch (err) {
      setError(extractErrorMessage(err));
    }
  }

  async function handleDeleteItem(itemId: number) {
    if (!plan) return;
    setConfirmAction({
      title: "Remove Plan Item",
      message: "Remove this plan item?",
      variant: "danger",
      action: async () => {
        setError("");
        try {
          const p = await apiFetch<ForecastPlan>(
            `/api/v1/forecast-plans/${plan.id}/items/${itemId}`,
            { method: "DELETE" }
          );
          await mutatePlan(p, { revalidate: false });
        } catch (err) { setError(extractErrorMessage(err)); }
      },
    });
  }

  async function handleActivate() {
    if (!plan) return;
    setConfirmAction({
      title: "Finalize Plan",
      message: "Finalize this plan? It will become read-only. You can revert to draft later if needed.",
      variant: "warning",
      action: async () => {
        setError("");
        try {
          const p = await apiFetch<ForecastPlan>(
            `/api/v1/forecast-plans/${plan.id}/activate`,
            { method: "POST" }
          );
          await mutatePlan(p, { revalidate: false });
        } catch (err) { setError(extractErrorMessage(err)); }
      },
    });
  }

  async function handleRevertToDraft() {
    if (!plan) return;
    setConfirmAction({
      title: "Revert to Draft",
      message: "Revert to draft? This will unlock the plan for editing.",
      variant: "warning",
      action: async () => {
        setError("");
        try {
          const p = await apiFetch<ForecastPlan>(
            `/api/v1/forecast-plans/${plan.id}/revert`,
            { method: "POST" }
          );
          await mutatePlan(p, { revalidate: false });
        } catch (err) { setError(extractErrorMessage(err)); }
      },
    });
  }

  async function handleDiscard() {
    if (!plan) return;
    setConfirmAction({
      title: "Discard Plan",
      message: "Discard this plan? All items will be removed and the plan will reset to an empty draft.",
      variant: "danger",
      action: async () => {
        setError("");
        try {
          const p = await apiFetch<ForecastPlan>(
            `/api/v1/forecast-plans/${plan.id}/discard`,
            { method: "POST" }
          );
          await mutatePlan(p, { revalidate: false });
          setShowForm(false);
        } catch (err) { setError(extractErrorMessage(err)); }
      },
    });
  }

  // Chart data. Memoized so the BarChart only re-layouts when the underlying
  // expense items change, not on every parent render (period nav, form
  // toggles, details-toggle, etc.). `categoryId` is preserved so the Cells
  // below can use a stable key instead of the array index.
  const chartData = useMemo(
    () =>
      expenseGroups.map((g) => ({
        categoryId: g.masterId,
        name: g.masterName,
        planned: g.planned,
        actual: g.actual,
      })),
    [expenseGroups],
  );

  const plannedNet =
    Number(plan?.total_planned_income ?? 0) -
    Number(plan?.total_planned_expense ?? 0);
  const actualNet =
    Number(plan?.total_actual_income ?? 0) -
    Number(plan?.total_actual_expense ?? 0);

  // Show the spinner whenever SWR is mid-flight OR we have a selected
  // period but no plan whose `period_start` matches it. The second clause
  // matters during period navigation: SWR's `keepPreviousData` keeps the
  // previous period's data in `planData`, but we deliberately blank it
  // out in the `plan` derivation above so action handlers can't fire
  // against the wrong plan ID. The page falls back to its loading state
  // in that window instead of letting buttons act on stale state.
  const fetching =
    planLoading || (periodStart !== "" && !plan);

  return (
    <AppShell>
      {/* Header */}
      <div className="mb-2 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-start gap-1">
          <h1 className={`${pageTitle} mb-0`}>Forecast Plans</h1>
          <HelpAnchor section="forecast-plans" label="Forecast Plans" />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {/* Show/hide details toggle. Hides variance/source/chart/refresh
              and the technical (?) help markers when off. */}
          <HelpTooltip
            content="Show details reveals variance vs plan, source breakdowns, the chart, and refresh from sources. Hide details keeps the page light."
            learnMoreSection="forecast-plans"
            triggerLabel="What does Show details include?"
          />
          <button
            type="button"
            role="switch"
            aria-checked={showDetails}
            aria-label={showDetails ? "Hide details" : "Show details"}
            onClick={() => setShowDetails((v) => !v)}
            className="inline-flex items-center gap-2 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-text-secondary hover:bg-surface-raised focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
          >
            <span
              aria-hidden="true"
              className={`inline-block h-3 w-6 rounded-full transition-colors ${
                showDetails ? "bg-accent" : "bg-border"
              }`}
            >
              <span
                className={`block h-3 w-3 rounded-full bg-surface transition-transform ${
                  showDetails ? "translate-x-3" : "translate-x-0"
                }`}
              />
            </span>
            <span>{showDetails ? "Hide details" : "Show details"}</span>
          </button>

          {isDraft && (
            <>
              {hasItems && (
                <button
                  onClick={handleDiscard}
                  className="rounded-md px-3 py-2 text-xs text-text-muted hover:text-danger"
                >
                  Discard
                </button>
              )}
              <button
                onClick={handlePopulate}
                className={btnPrimary}
                title={
                  showDetails ? HELP_AUTO_POPULATE + DOCS_HINT : undefined
                }
              >
                Auto-populate
              </button>
              {showDetails && (
                <HelpIcon label="Auto-populate" text={HELP_AUTO_POPULATE} />
              )}
              {showDetails && hasItems && (
                <>
                  <button
                    onClick={handleRefreshFromSources}
                    className={btnPrimary}
                    title={HELP_REFRESH + DOCS_HINT}
                  >
                    Refresh from sources
                  </button>
                  <HelpIcon label="Refresh from sources" text={HELP_REFRESH} />
                </>
              )}
              <button
                onClick={() => setShowForm(!showForm)}
                className={btnPrimary}
              >
                {showForm ? "Cancel" : "+ Add Item"}
              </button>
            </>
          )}
          {isActive && (
            <>
              {showDetails && (
                <button
                  onClick={handleEditAndRefresh}
                  className={btnPrimary}
                  title={HELP_REFRESH + DOCS_HINT}
                >
                  Refresh from sources
                </button>
              )}
              <button
                onClick={handleRevertToDraft}
                className={btnPrimary}
                title={
                  showDetails ? HELP_EDIT_PLAN + DOCS_HINT : undefined
                }
              >
                Edit Plan
              </button>
              {showDetails && (
                <HelpIcon label="Edit Plan" text={HELP_EDIT_PLAN} />
              )}
            </>
          )}
        </div>
      </div>

      {/* Contextual guidance */}
      <p className="mb-5 text-xs text-text-muted leading-relaxed">
        {isFuturePeriod
          ? "Plan your expected income and expenses for this future period. Use Auto-populate to import from recurring templates and historical averages, then adjust manually."
          : isCurrentPeriod
            ? "Track your planned vs actual income and expenses for the current period. Actuals update automatically from settled transactions."
            : isPastPeriod
              ? "Review how your plan compared to actual results for this closed period."
              : "Set up your financial plan for this billing period."}
        {isDraft && hasItems && (
          <span className="ml-1">
            This plan is a <strong>draft</strong>. Finalize it when you&apos;re done editing.
          </span>
        )}
        {isActive && (
          <span className="ml-1">
            This plan is <strong>finalized</strong>. Click <strong>Edit Plan</strong> to make changes.
          </span>
        )}
      </p>

      {/* Build granularity control (admin-only, org-scoped). Lets the
          owner choose whether forecasts are built from master categories
          or from subcategories that sum into their master. */}
      {admin && (
        <div className="mb-5 flex flex-wrap items-center gap-2">
          <span className="text-xs font-medium text-text-secondary">
            Build forecasts by:
          </span>
          <div
            role="radiogroup"
            aria-label="Forecast build granularity"
            className="inline-flex overflow-hidden rounded-md border border-border"
          >
            {(["master", "subcategory"] as const).map((m) => (
              <button
                key={m}
                type="button"
                role="radio"
                aria-checked={mode === m}
                disabled={savingMode}
                onClick={() => handleSetMode(m)}
                className={`px-3 py-1.5 text-xs font-medium transition-colors disabled:opacity-50 ${
                  mode === m
                    ? "bg-accent text-accent-text"
                    : "text-text-muted hover:bg-surface-raised"
                }`}
              >
                {m === "master" ? "Master categories" : "Subcategories"}
              </button>
            ))}
          </div>
          <HelpIcon
            label="Build granularity"
            text="Master builds one forecast row per master category. Subcategories let a master's forecast be built from multiple subcategory rows that sum into the master. Applies to everyone in your organization."
          />
        </div>
      )}

      {/* Period navigation */}
      {periods.length > 0 && (
        <div className="mb-5 flex items-center gap-3">
          <button
            onClick={() =>
              setPeriodIdx(Math.min(periodIdx + 1, periods.length - 1))
            }
            disabled={periodIdx >= periods.length - 1}
            className="inline-flex min-h-[44px] min-w-[44px] items-center justify-center rounded p-1 text-text-muted hover:bg-surface-raised disabled:opacity-30 md:min-h-0 md:min-w-0"
            aria-label="Older period"
          >
            <svg
              className="h-4 w-4"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M15.75 19.5 8.25 12l7.5-7.5"
              />
            </svg>
          </button>
          <span className="text-sm text-text-secondary">
            {selectedPeriod?.start_date}
            {selectedPeriod?.end_date
              ? ` – ${selectedPeriod.end_date}`
              : ""}
            {isCurrentPeriod && (
              <span className="ml-2 text-xs font-medium text-success">
                current
              </span>
            )}
            {isFuturePeriod && (
              <span className="ml-2 text-xs font-medium text-accent">
                future
              </span>
            )}
          </span>
          <button
            onClick={() => setPeriodIdx(Math.max(periodIdx - 1, 0))}
            disabled={periodIdx <= 0}
            className="inline-flex min-h-[44px] min-w-[44px] items-center justify-center rounded p-1 text-text-muted hover:bg-surface-raised disabled:opacity-30 md:min-h-0 md:min-w-0"
            aria-label="Newer period"
          >
            <svg
              className="h-4 w-4"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="m8.25 4.5 7.5 7.5-7.5 7.5"
              />
            </svg>
          </button>
          {!isCurrentPeriod && (
            <button
              onClick={() => {
                const idx = periods.findIndex((p) => p.end_date === null);
                if (idx >= 0) setPeriodIdx(idx);
              }}
              className="ml-1 rounded-md px-2 py-1 text-[11px] font-medium text-text-muted hover:bg-surface-raised"
            >
              Today
            </button>
          )}
          {plan && (
            <span
              className={`rounded-full px-2.5 py-0.5 text-[11px] font-medium ${
                isActive
                  ? "bg-success/15 text-success"
                  : "bg-accent/15 text-accent"
              }`}
            >
              {isActive ? "Finalized" : "Draft"}
            </span>
          )}
        </div>
      )}

      {error && <div className={`mb-6 ${errorCls}`}>{error}</div>}

      {refreshError && (
        <div
          className={`mb-6 flex items-center justify-between gap-3 ${errorCls}`}
          role="status"
          data-testid="forecast-plans-refresh-error"
        >
          <span>Failed to refresh after the last update. Try again.</span>
          <button
            type="button"
            onClick={() => {
              setRefreshError(false);
              void refreshAfterTransactionAdded();
            }}
            disabled={refreshing}
            className="rounded-md border border-danger/40 px-3 py-1 text-xs font-medium text-danger hover:bg-danger/10 disabled:opacity-50"
          >
            {refreshing ? "Retrying..." : "Retry"}
          </button>
        </div>
      )}

      {/* Add item form (draft only) */}
      {showForm && isDraft && (
        <div className={`mb-6 ${card} p-6`}>
          <form
            onSubmit={handleAddItem}
            className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-end sm:gap-4"
          >
            <div className="w-full sm:w-32">
              <label htmlFor="fp-type" className={label}>
                Type
              </label>
              <select
                id="fp-type"
                value={formType}
                onChange={(e) => {
                  // Clear the in-progress category pick — the previously
                  // selected expense category would otherwise ride along
                  // into an income POST and trip the backend's
                  // type-mismatch guard.
                  setFormType(e.target.value as "income" | "expense");
                  setFormCategoryId("");
                }}
                className={input}
              >
                <option value="expense">Expense</option>
                <option value="income">Income</option>
              </select>
            </div>
            <div className="w-full sm:min-w-[200px] sm:flex-1">
              <label htmlFor="fp-cat" className={label}>
                Category
              </label>
              <CategorySelect
                id="fp-cat"
                categories={categories}
                value={formCategoryId}
                onChange={(id) => setFormCategoryId(id)}
                filterType={formType}
                disabledIds={disabledForType}
                className={input}
                aria-label="Plan item category"
                onCategoryCreated={(cat) => {
                  setCategories((prev) =>
                    prev.some((c) => c.id === cat.id) ? prev : [...prev, cat],
                  );
                }}
              />
            </div>
            <div className="w-full sm:w-40">
              <label htmlFor="fp-amount" className={label}>
                Planned Amount
              </label>
              <input
                id="fp-amount"
                type="number"
                step="0.01"
                min="0.01"
                required
                placeholder="0.00"
                value={formAmount}
                onChange={(e) => setFormAmount(e.target.value)}
                className={input}
              />
            </div>
            <button
              type="submit"
              disabled={savingMode}
              className={`${btnPrimary} w-full sm:w-auto sm:min-h-0 disabled:opacity-50`}
            >
              Add
            </button>
          </form>
        </div>
      )}

      {fetching ? (
        <Spinner />
      ) : (
        <div className="space-y-6">
          {/* Summary cards */}
          {plan && hasItems && (
            <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
              <div className={`${card} p-5`}>
                <p className={cardTitle}>Planned Income</p>
                <p className="mt-1 text-xl font-semibold tabular-nums text-success">
                  {formatAmount(plan.total_planned_income)}
                </p>
                <p className="mt-0.5 text-xs text-text-muted">
                  Actual: {formatAmount(plan.total_actual_income)}
                </p>
              </div>
              <div className={`${card} p-5`}>
                <p className={cardTitle}>Planned Expenses</p>
                <p className="mt-1 text-xl font-semibold tabular-nums text-danger">
                  {formatAmount(plan.total_planned_expense)}
                </p>
                <p className="mt-0.5 text-xs text-text-muted">
                  Actual: {formatAmount(plan.total_actual_expense)}
                </p>
              </div>
              <div className={`${card} p-5`}>
                <p className={cardTitle}>Planned Net</p>
                <p
                  className={`mt-1 text-xl font-semibold tabular-nums ${plannedNet >= 0 ? "text-success" : "text-danger"}`}
                >
                  {formatAmount(plannedNet)}
                </p>
              </div>
              <div className={`${card} p-5`}>
                <p className={cardTitle}>Actual Net</p>
                <p
                  className={`mt-1 text-xl font-semibold tabular-nums ${actualNet >= 0 ? "text-success" : "text-danger"}`}
                >
                  {formatAmount(actualNet)}
                </p>
              </div>
            </div>
          )}

          {/* Planned vs Actual chart */}
          {showDetails && chartData.length > 0 && (
            <div className={`${card} p-5 overflow-hidden`}>
              <h2 className={`${cardTitle} mb-4`}>
                Planned vs Actual (Expenses)
              </h2>
              <div className="w-full min-w-0" style={{ height: Math.max(chartData.length * 40, 100) }}>
                <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 1, height: 1 }}>
                  <BarChart
                    data={chartData}
                    layout="vertical"
                    margin={{ left: 0, right: 20, top: 0, bottom: 0 }}
                  >
                    <XAxis type="number" hide />
                    <YAxis
                      type="category"
                      dataKey="name"
                      width={100}
                      tick={{ fill: chartColor.axisTick, fontSize: 11 }}
                    />
                    <Tooltip
                      formatter={(v, name) => [
                        formatAmount(Number(v)),
                        name === "planned" ? <span style={{ color: chartColor.planned }}>Planned</span> : <span style={{ color: chartColor.actual }}>Actual</span>,
                      ]}
                      contentStyle={{ fontSize: "11px" }}
                    />
                    <Bar
                      dataKey="planned"
                      fill={chartColor.planned}
                      radius={[4, 4, 4, 4]}
                      animationDuration={600}
                      cursor="pointer"
                      onClick={(data) => {
                        const name = data?.name || data?.payload?.name;
                        if (name) router.push(`/transactions?category=${encodeURIComponent(name)}`);
                      }}
                    />
                    <Bar
                      dataKey="actual"
                      fill={chartColor.actual}
                      radius={[4, 4, 4, 4]}
                      animationDuration={600}
                    >
                      {chartData.map((d) => (
                        <Cell
                          key={d.categoryId}
                          fill={d.actual > d.planned ? chartColor.over : chartColor.actual}
                        />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
              <div className="mt-3 flex gap-4 px-4 pb-2 text-[10px] text-text-muted">
                <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: chartColor.planned }} /> Planned</span>
                <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: chartColor.actual }} /> Under plan</span>
                <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: chartColor.over }} /> Over plan</span>
              </div>
            </div>
          )}

          {/* Filter tabs */}
          {plan && hasItems && (
            <div className="flex gap-1">
              {(["all", "expense", "income"] as const).map((f) => (
                <button
                  key={f}
                  onClick={() => setViewFilter(f)}
                  className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
                    viewFilter === f
                      ? "bg-accent text-accent-text"
                      : "text-text-muted hover:bg-surface-raised"
                  }`}
                >
                  {f === "all"
                    ? "All"
                    : f === "income"
                      ? "Income"
                      : "Expenses"}
                </button>
              ))}
            </div>
          )}

          {/* Item lists */}
          {(viewFilter === "all" || viewFilter === "income") &&
            incomeItems.length > 0 && (
              <ItemSection
                title="Income"
                groups={incomeGroups}
                readOnly={isActive}
                showDetails={showDetails}
                editingId={editingId}
                editAmount={editAmount}
                onStartEdit={(item) => {
                  setEditingId(item.id);
                  setEditAmount(String(item.planned_amount));
                }}
                onCancelEdit={() => setEditingId(null)}
                onSaveEdit={handleUpdateItem}
                onDelete={handleDeleteItem}
                setEditAmount={setEditAmount}
              />
            )}

          {(viewFilter === "all" || viewFilter === "expense") &&
            expenseItems.length > 0 && (
              <ItemSection
                title="Expenses"
                groups={expenseGroups}
                readOnly={isActive}
                showDetails={showDetails}
                editingId={editingId}
                editAmount={editAmount}
                onStartEdit={(item) => {
                  setEditingId(item.id);
                  setEditAmount(String(item.planned_amount));
                }}
                onCancelEdit={() => setEditingId(null)}
                onSaveEdit={handleUpdateItem}
                onDelete={handleDeleteItem}
                setEditAmount={setEditAmount}
              />
            )}

          {/* Empty state */}
          {plan && !hasItems && (
            <div className={`${card} px-6 py-12 text-center`}>
              <p className="text-sm text-text-muted">
                No plan items yet. Click{" "}
                <strong>&quot;Auto-populate&quot;</strong> to import from
                recurring templates and historical averages, or{" "}
                <strong>&quot;+ Add Item&quot;</strong> to add manually.
              </p>
            </div>
          )}

          {/* Bottom actions */}
          {plan && hasItems && isDraft && (
            <div className="flex justify-end">
              <button onClick={handleActivate} className={btnPrimary}>
                Finalize Plan
              </button>
            </div>
          )}
        </div>
      )}
      <ConfirmModal
        open={confirmAction !== null}
        title={confirmAction?.title ?? ""}
        message={confirmAction?.message ?? ""}
        confirmLabel={confirmAction?.confirmLabel ?? "Confirm"}
        variant={confirmAction?.variant ?? "default"}
        onConfirm={() => { confirmAction?.action(); setConfirmAction(null); }}
        onCancel={() => setConfirmAction(null)}
      />
    </AppShell>
  );
}

/* ── Item section component ──────────────────────────────────────────────── */

function ItemSection({
  title,
  groups,
  readOnly,
  showDetails,
  editingId,
  editAmount,
  onStartEdit,
  onCancelEdit,
  onSaveEdit,
  onDelete,
  setEditAmount,
}: {
  title: string;
  groups: MasterGroup[];
  readOnly: boolean;
  showDetails: boolean;
  editingId: number | null;
  editAmount: string;
  onStartEdit: (item: ForecastPlanItem) => void;
  onCancelEdit: () => void;
  onSaveEdit: (id: number) => void;
  onDelete: (id: number) => void;
  setEditAmount: (v: string) => void;
}) {
  // When details are off, drop variance + source columns. The grid
  // template tracks the visible column count so cells don't wrap
  // around an invisible slot.
  const colTemplate = readOnly
    ? showDetails
      ? "grid-cols-[1fr_100px] md:grid-cols-[1fr_100px_100px_100px_80px]"
      : "grid-cols-[1fr_100px] md:grid-cols-[1fr_100px_100px]"
    : showDetails
      ? "grid-cols-[1fr_100px_100px] md:grid-cols-[1fr_100px_100px_100px_80px_100px]"
      : "grid-cols-[1fr_100px_100px] md:grid-cols-[1fr_100px_100px_100px]";

  // Renders one editable/display row for a single plan item. Subcategory
  // rows pass `indented` so they sit visually under their master header.
  function renderItemRow(item: ForecastPlanItem, indented: boolean) {
    const variance = Number(item.variance);
    const isOver = item.type === "expense" ? variance > 0 : variance < 0;
    return (
      <div
        key={item.id}
        className={`grid ${colTemplate} items-center gap-2 ${
          indented ? "pl-10 pr-6" : "px-6"
        } py-2.5`}
      >
                  {!readOnly && editingId === item.id ? (
                    <>
                      <div className="text-sm text-text-primary">
                        {item.category_name}
                        <div className="md:hidden mt-1 text-xs text-text-muted">
                          Actual {formatAmount(item.actual_amount)}
                        </div>
                      </div>
                      <input
                        type="number"
                        step="0.01"
                        min="0.01"
                        value={editAmount}
                        onChange={(e) => setEditAmount(e.target.value)}
                        className={`text-right ${input}`}
                        autoFocus
                        onKeyDown={(e) => {
                          if (e.key === "Enter") onSaveEdit(item.id);
                          if (e.key === "Escape") onCancelEdit();
                        }}
                      />
                      <span className="hidden text-right text-sm tabular-nums text-text-secondary md:block">
                        {formatAmount(item.actual_amount)}
                      </span>
                      {showDetails && (
                        <>
                          <span className="hidden md:block" />
                          <span className="hidden md:block" />
                        </>
                      )}
                      <div className="flex justify-end gap-2">
                        {/* Save + Cancel rendered as proper buttons (PR
                            #forecast-plans-cancel). Before, both were
                            `text-xs` link-style controls and the Cancel
                            in particular was nearly invisible — the
                            user's only obvious bail-out from an edit
                            session was the page-level Discard, which
                            wipes the whole plan. Keep the inline edit
                            local (no server call) so opening a row by
                            mistake and clicking Cancel just restores
                            the row to its persisted state. */}
                        <button
                          type="button"
                          onClick={onCancelEdit}
                          className="rounded-md border border-border px-2 py-1 text-xs font-medium text-text-secondary hover:bg-surface-raised"
                          data-testid={`fp-edit-cancel-${item.id}`}
                        >
                          Cancel
                        </button>
                        <button
                          type="button"
                          onClick={() => onSaveEdit(item.id)}
                          className="rounded-md bg-accent px-2 py-1 text-xs font-medium text-accent-text hover:bg-accent-hover"
                          data-testid={`fp-edit-save-${item.id}`}
                        >
                          Save
                        </button>
                      </div>
                    </>
                  ) : (
                    <>
                      <div className="text-sm text-text-primary">
                        {item.category_name}
                        <div className="md:hidden mt-1 text-xs text-text-muted">
                          Actual {formatAmount(item.actual_amount)}
                          {showDetails && (
                            <>
                              {" · "}Variance{" "}
                              <span
                                className={`font-medium ${
                                  isOver ? "text-danger" : "text-success"
                                }`}
                              >
                                {variance > 0 ? "+" : ""}
                                {formatAmount(variance)}
                              </span>
                              {" · "}
                              {SOURCE_LABELS[item.source] ?? item.source}
                            </>
                          )}
                        </div>
                      </div>
                      <span className="text-right text-sm tabular-nums text-text-primary">
                        {formatAmount(item.planned_amount)}
                      </span>
                      <span className="hidden text-right text-sm tabular-nums text-text-secondary md:block">
                        {formatAmount(item.actual_amount)}
                      </span>
                      {showDetails && (
                        <>
                          <span
                            className={`hidden text-right text-sm tabular-nums font-medium md:block ${
                              isOver ? "text-danger" : "text-success"
                            }`}
                          >
                            {variance > 0 ? "+" : ""}
                            {formatAmount(variance)}
                          </span>
                          <span className="hidden text-center text-[11px] text-text-muted md:block">
                            {SOURCE_LABELS[item.source] ?? item.source}
                          </span>
                        </>
                      )}
                      {!readOnly && (
                        <div className="flex justify-end gap-2">
                          <button
                            onClick={() => onStartEdit(item)}
                            className={btnLink}
                          >
                            Edit
                          </button>
                          <button
                            onClick={() => onDelete(item.id)}
                            className={btnDanger}
                          >
                            Remove
                          </button>
                        </div>
                      )}
                    </>
                  )}
      </div>
    );
  }

  // Renders a master-level roll-up header for a group built from
  // subcategory items: name + summed planned/actual/variance, no actions
  // (the master is edited via its subs). The forecast is always reported
  // at the master level (product decision).
  function renderMasterHeader(group: MasterGroup) {
    const isOver =
      title === "Expenses" ? group.variance > 0 : group.variance < 0;
    return (
      <div
        className={`grid ${colTemplate} items-center gap-2 bg-surface-raised/40 px-6 py-2.5`}
        data-testid={`fp-master-${group.masterId}`}
      >
        <div className="text-sm font-semibold text-text-primary">
          {group.masterName}
          <div className="md:hidden mt-1 text-xs font-normal text-text-muted">
            Actual {formatAmount(group.actual)}
          </div>
        </div>
        <span className="text-right text-sm font-semibold tabular-nums text-text-primary">
          {formatAmount(group.planned)}
        </span>
        <span className="hidden text-right text-sm font-semibold tabular-nums text-text-secondary md:block">
          {formatAmount(group.actual)}
        </span>
        {showDetails && (
          <>
            <span
              className={`hidden text-right text-sm font-semibold tabular-nums md:block ${
                isOver ? "text-danger" : "text-success"
              }`}
            >
              {group.variance > 0 ? "+" : ""}
              {formatAmount(group.variance)}
            </span>
            <span className="hidden md:block" />
          </>
        )}
        {!readOnly && <span />}
      </div>
    );
  }

  return (
    <div className={card}>
      <div className={cardHeader}>
        <h2 className={cardTitle}>{title}</h2>
      </div>
      <div className="overflow-x-auto">
        <div className="min-w-[320px]">
          {/* Header row */}
          <div
            className={`grid ${colTemplate} gap-2 px-6 py-2 text-[11px] font-semibold uppercase tracking-wider text-text-muted`}
          >
            <span>Category</span>
            <span className="text-right">Planned</span>
            <span className="hidden text-right md:block">Actual</span>
            {showDetails && (
              <>
                <span className="hidden text-right md:block">
                  Variance
                  <HelpIcon label="Variance" text={HELP_VARIANCE} />
                </span>
                <span className="hidden text-center md:block">Source</span>
              </>
            )}
            {!readOnly && <span className="text-right">Actions</span>}
          </div>
          <div className="divide-y divide-border-subtle">
            {groups.map((group) =>
              group.isMasterLevel ? (
                renderItemRow(group.subItems[0], false)
              ) : (
                <div key={`group-${group.masterId}`}>
                  {renderMasterHeader(group)}
                  {group.subItems.map((sub) => renderItemRow(sub, true))}
                </div>
              ),
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
