"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import SystemLayout from "@/components/SystemLayout";
import ConfirmModal from "@/components/ui/ConfirmModal";
import DuplicatePlanModal from "@/components/system/DuplicatePlanModal";
import Pagination from "@/components/ui/Pagination";
import SortableHeader from "@/components/ui/SortableHeader";
import { useTableState } from "@/lib/hooks/use-table-state";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { hasPlatformPermission } from "@/lib/auth";
import { FEATURE_LABELS } from "@/lib/feature-catalog";
import {
  input,
  label,
  btnPrimary,
  btnSecondary,
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
  success as successCls,
  pageTitle,
} from "@/lib/styles";
import type { FeatureKey, ListEnvelope, Plan, PlanFeatures } from "@/lib/types";

const DEFAULT_FEATURES: PlanFeatures = {
  "ai.budget": false,
  "ai.forecast": false,
  "ai.smart_plan": false,
  "ai.autocategorize": false,
};

const FEATURE_KEYS = Object.keys(FEATURE_LABELS) as FeatureKey[];

interface PlanWithCount extends Plan {
  org_count?: number;
}

// Backend-whitelisted sort keys for /api/v1/plans/all. Limited to the
// columns the table exposes as headers.
const PLAN_SORT_FIELDS = [
  "name",
  "price_monthly",
  "price_yearly",
  "max_users",
  "retention_days",
  "is_active",
] as const;
type PlanSortField = (typeof PLAN_SORT_FIELDS)[number];

export default function SystemPlansPage() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const [plans, setPlans] = useState<PlanWithCount[]>([]);
  const [total, setTotal] = useState(0);
  const { sortField, sortDir, setSort, page, setPage, pageSize, setPageSize } =
    useTableState<PlanSortField>({
      key: "system-plans",
      defaultSortField: "name",
      defaultSortDir: "asc",
      allowedSortFields: PLAN_SORT_FIELDS,
    });
  const [error, setError] = useState("");
  const [successMsg, setSuccessMsg] = useState("");
  const [editing, setEditing] = useState<PlanWithCount | null>(null);
  const [creating, setCreating] = useState(false);
  const [duplicateSource, setDuplicateSource] = useState<PlanWithCount | null>(null);
  const [confirmAction, setConfirmAction] = useState<{
    title: string;
    message: string;
    variant: "warning" | "danger";
    action: () => void;
  } | null>(null);

  const [formName, setFormName] = useState("");
  const [formSlug, setFormSlug] = useState("");
  const [formDescription, setFormDescription] = useState("");
  const [formPriceMonthly, setFormPriceMonthly] = useState("0");
  const [formPriceYearly, setFormPriceYearly] = useState("0");
  const [formMaxUsers, setFormMaxUsers] = useState("");
  const [formRetentionDays, setFormRetentionDays] = useState("");
  const [formIsCustom, setFormIsCustom] = useState(false);
  const [formSortOrder, setFormSortOrder] = useState("0");
  const [formFeatures, setFormFeatures] = useState<PlanFeatures>({ ...DEFAULT_FEATURES });

  // Forward-compatible gate: today /me does not return a permissions
  // array, so non-superadmins resolve to false (matching the previous
  // is_superadmin short-circuit). The backend gate on plans.manage is
  // still authoritative — this client guard just avoids flashing the
  // page shell before the redirect.
  const canManagePlans = hasPlatformPermission(user, "plans.manage");

  // Two-branch guard: AppShell can't redirect from a null render, so we
  // explicitly send unauthenticated visitors to /login and authenticated
  // users without plans.manage to /dashboard.
  useEffect(() => {
    if (loading) return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (!canManagePlans) {
      router.replace("/dashboard");
      return;
    }
  }, [loading, user, canManagePlans, router]);

  const loadPlans = useCallback(async () => {
    try {
      const params = new URLSearchParams({
        sort_by: sortField,
        sort_dir: sortDir,
        limit: String(pageSize),
        offset: String((page - 1) * pageSize),
      });
      const data = await apiFetch<ListEnvelope<PlanWithCount>>(
        `/api/v1/plans/all?${params.toString()}`,
      );
      setPlans(data.items);
      setTotal(data.total);
    } catch (err) {
      setError(extractErrorMessage(err));
    }
  }, [sortField, sortDir, page, pageSize]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data fetch: loadPlans() writes the plans page + total into state when sort/page/pageSize change
    if (canManagePlans) void loadPlans();
  }, [canManagePlans, loadPlans]);

  const handleSort = useCallback(
    (field: string) => {
      if (!(PLAN_SORT_FIELDS as readonly string[]).includes(field)) return;
      const f = field as PlanSortField;
      setSort(f, f === sortField && sortDir === "asc" ? "desc" : "asc");
    },
    [sortField, sortDir, setSort],
  );

  function resetForm() {
    setFormName("");
    setFormSlug("");
    setFormDescription("");
    setFormPriceMonthly("0");
    setFormPriceYearly("0");
    setFormMaxUsers("");
    setFormRetentionDays("");
    setFormIsCustom(false);
    setFormSortOrder("0");
    setFormFeatures({ ...DEFAULT_FEATURES });
  }

  function openEdit(plan: PlanWithCount) {
    setEditing(plan);
    setCreating(false);
    setFormName(plan.name);
    setFormSlug(plan.slug);
    setFormDescription(plan.description);
    setFormPriceMonthly(String(plan.price_monthly));
    setFormPriceYearly(String(plan.price_yearly));
    setFormMaxUsers(plan.max_users != null ? String(plan.max_users) : "");
    setFormRetentionDays(plan.retention_days != null ? String(plan.retention_days) : "");
    setFormIsCustom(plan.is_custom);
    setFormSortOrder(String(plan.sort_order));
    setFormFeatures({ ...DEFAULT_FEATURES, ...(plan.features ?? {}) });
  }

  function openCreate() {
    setEditing(null);
    setCreating(true);
    resetForm();
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    const common = {
      name: formName,
      description: formDescription,
      price_monthly: parseFloat(formPriceMonthly) || 0,
      price_yearly: parseFloat(formPriceYearly) || 0,
      max_users: formMaxUsers ? parseInt(formMaxUsers) : null,
      retention_days: formRetentionDays ? parseInt(formRetentionDays) : null,
      is_custom: formIsCustom,
      sort_order: parseInt(formSortOrder) || 0,
      features: formFeatures,
    };

    try {
      if (editing) {
        // PlanUpdate forbids extra fields and has no `slug` field — omit it on edit.
        await apiFetch(`/api/v1/plans/${editing.id}`, {
          method: "PUT",
          body: JSON.stringify(common),
        });
        setSuccessMsg("Plan updated");
      } else {
        await apiFetch("/api/v1/plans", {
          method: "POST",
          body: JSON.stringify({ ...common, slug: formSlug }),
        });
        setSuccessMsg("Plan created");
      }
      setTimeout(() => setSuccessMsg(""), 3000);
      setEditing(null);
      setCreating(false);
      resetForm();
      await loadPlans();
    } catch (err) {
      setError(extractErrorMessage(err));
    }
  }

  function handleDelete(plan: PlanWithCount) {
    setConfirmAction({
      title: "Deactivate Plan",
      message: `Deactivate "${plan.name}"? This will fail if any organizations are currently on this plan.`,
      variant: "danger",
      action: async () => {
        setError("");
        try {
          await apiFetch(`/api/v1/plans/${plan.id}`, { method: "DELETE" });
          setSuccessMsg("Plan deactivated");
          setTimeout(() => setSuccessMsg(""), 3000);
          await loadPlans();
        } catch (err) {
          setError(extractErrorMessage(err));
        }
      },
    });
  }

  if (loading || !canManagePlans) return null;

  return (
    <SystemLayout current="Plan Catalog">
      <div className="flex flex-col gap-2 mb-8 sm:flex-row sm:items-center sm:justify-between">
        <h1 className={pageTitle + " mb-0"}>Plan Management</h1>
        <button onClick={openCreate} className={`${btnPrimary} w-full sm:w-auto sm:min-h-0`}>+ New Plan</button>
      </div>

      {error && <p className={`${errorCls} mb-4`}>{error}</p>}
      {successMsg && <p className={`${successCls} mb-4`}>{successMsg}</p>}

      {(creating || editing) && (
        <div className={`${card} mb-6`}>
          <div className={cardHeader}>
            <h2 className={cardTitle}>{editing ? `Edit: ${editing.name}` : "New Plan"}</h2>
          </div>
          <form onSubmit={handleSubmit} className="p-6 grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div>
              <label htmlFor="plan-name" className={label}>Name</label>
              <input id="plan-name" value={formName} onChange={(e) => setFormName(e.target.value)} className={input} required />
            </div>
            <div>
              <label htmlFor="plan-slug" className={label}>Slug</label>
              <input
                id="plan-slug"
                value={formSlug}
                onChange={(e) => setFormSlug(e.target.value)}
                className={input}
                pattern={"[a-z0-9\\-]+"}
                required
                disabled={!!editing}
              />
            </div>
            <div className="sm:col-span-2">
              <label htmlFor="plan-description" className={label}>Description</label>
              <input id="plan-description" value={formDescription} onChange={(e) => setFormDescription(e.target.value)} className={input} />
            </div>
            <div>
              <label htmlFor="plan-price-monthly" className={label}>Price Monthly (€)</label>
              <input id="plan-price-monthly" type="number" step="0.01" min="0" value={formPriceMonthly} onChange={(e) => setFormPriceMonthly(e.target.value)} className={input} />
            </div>
            <div>
              <label htmlFor="plan-price-yearly" className={label}>Price Yearly (€)</label>
              <input id="plan-price-yearly" type="number" step="0.01" min="0" value={formPriceYearly} onChange={(e) => setFormPriceYearly(e.target.value)} className={input} />
            </div>
            <div>
              <label htmlFor="plan-max-users" className={label}>Max Users (blank = unlimited)</label>
              <input id="plan-max-users" type="number" min="1" value={formMaxUsers} onChange={(e) => setFormMaxUsers(e.target.value)} className={input} />
            </div>
            <div>
              <label htmlFor="plan-retention-days" className={label}>Retention Days (blank = unlimited)</label>
              <input id="plan-retention-days" type="number" min="1" value={formRetentionDays} onChange={(e) => setFormRetentionDays(e.target.value)} className={input} />
            </div>
            <div>
              <label htmlFor="plan-sort-order" className={label}>Sort Order</label>
              <input id="plan-sort-order" type="number" value={formSortOrder} onChange={(e) => setFormSortOrder(e.target.value)} className={input} />
            </div>
            <div className="flex items-center gap-2 pt-6">
              <input type="checkbox" id="is_custom" checked={formIsCustom} onChange={(e) => setFormIsCustom(e.target.checked)} />
              <label htmlFor="is_custom" className="text-sm text-text-secondary">Custom plan</label>
            </div>
            <div className="col-span-1 sm:col-span-2 mt-2 border-t border-border pt-4">
              <h3 className="mb-3 text-sm font-semibold text-text-primary">Features</h3>
              <div className="flex flex-col gap-3">
                {FEATURE_KEYS.map((key) => {
                  const meta = FEATURE_LABELS[key];
                  const inputId = `feature-${key}`;
                  return (
                    <div key={key} className="flex items-start gap-3">
                      <input
                        type="checkbox"
                        id={inputId}
                        className="mt-1"
                        checked={formFeatures[key] ?? false}
                        onChange={(e) =>
                          setFormFeatures((prev) => ({ ...prev, [key]: e.target.checked }))
                        }
                      />
                      <label htmlFor={inputId} className="flex flex-col text-sm">
                        <span className="font-medium text-text-primary">{meta.label}</span>
                        <span className="text-xs text-text-muted">{meta.description}</span>
                      </label>
                    </div>
                  );
                })}
              </div>
            </div>
            <div className="col-span-1 sm:col-span-2 flex flex-col-reverse gap-2 pt-2 sm:flex-row sm:justify-end sm:gap-3">
              <button
                type="button"
                onClick={() => { setEditing(null); setCreating(false); resetForm(); }}
                className={`${btnSecondary} w-full sm:w-auto min-h-[44px] sm:min-h-0`}
              >
                Cancel
              </button>
              <button type="submit" className={`${btnPrimary} w-full sm:w-auto sm:min-h-0`}>{editing ? "Save" : "Create"}</button>
            </div>
          </form>
        </div>
      )}

      <div className={`${card} w-full`}>
        <div className="w-full overflow-x-auto">
          <table className="w-full min-w-[640px] text-sm">
            <thead>
              <tr className="border-b border-border text-left">
                <SortableHeader
                  label="Plan"
                  field="name"
                  activeField={sortField}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <SortableHeader
                  label="Monthly"
                  field="price_monthly"
                  activeField={sortField}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <SortableHeader
                  label="Yearly"
                  field="price_yearly"
                  activeField={sortField}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <SortableHeader
                  label="Max Users"
                  field="max_users"
                  activeField={sortField}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <SortableHeader
                  label="Retention"
                  field="retention_days"
                  activeField={sortField}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <SortableHeader
                  label="Status"
                  field="is_active"
                  activeField={sortField}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody>
              {plans.map((plan) => (
                <tr key={plan.id} className="border-b border-border">
                  <td className="px-4 py-3">
                    <div className="font-medium text-text-primary">{plan.name}</div>
                    <div className="flex items-center gap-1 text-[11px] text-text-muted">
                      {plan.slug}
                      {plan.is_custom && (
                        <span className="inline-flex items-center rounded bg-warning-dim px-1.5 py-0.5 text-[10px] font-medium text-warning">CUSTOM</span>
                      )}
                    </div>
                  </td>
                  <td className="px-4 py-3 text-text-secondary">€{Number(plan.price_monthly).toFixed(2)}</td>
                  <td className="px-4 py-3 text-text-secondary">€{Number(plan.price_yearly).toFixed(2)}</td>
                  <td className="px-4 py-3 text-text-secondary">{plan.max_users ?? "∞"}</td>
                  <td className="px-4 py-3 text-text-secondary">{plan.retention_days ? `${plan.retention_days}d` : "∞"}</td>
                  <td className="px-4 py-3">
                    <span className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${plan.is_active ? "bg-success-dim text-success" : "bg-danger-dim text-danger"}`}>
                      {plan.is_active ? "Active" : "Inactive"}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right space-x-2">
                    <button onClick={() => openEdit(plan)} className="text-xs text-accent hover:underline">Edit</button>
                    <button onClick={() => setDuplicateSource(plan)} className="text-xs text-accent hover:underline">Duplicate</button>
                    {plan.is_active && (
                      <button onClick={() => handleDelete(plan)} className="text-xs text-text-muted hover:text-danger">Deactivate</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {(total > pageSize || page > 1) && (
          <div className="px-4">
            <Pagination
              page={page}
              pageSize={pageSize}
              total={total}
              onPageChange={setPage}
              onPageSizeChange={setPageSize}
            />
          </div>
        )}
      </div>

      <ConfirmModal
        open={!!confirmAction}
        title={confirmAction?.title ?? ""}
        message={confirmAction?.message ?? ""}
        variant={confirmAction?.variant ?? "warning"}
        onConfirm={() => {
          confirmAction?.action();
          setConfirmAction(null);
        }}
        onCancel={() => setConfirmAction(null)}
      />

      {duplicateSource && (
        <DuplicatePlanModal
          source={duplicateSource}
          onClose={() => setDuplicateSource(null)}
          onDuplicated={() => {
            setSuccessMsg("Plan duplicated");
            setTimeout(() => setSuccessMsg(""), 3000);
            void loadPlans();
          }}
        />
      )}
    </SystemLayout>
  );
}
