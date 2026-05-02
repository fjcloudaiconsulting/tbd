"use client";

import { useEffect, useState } from "react";

import { apiFetch, extractErrorMessage } from "@/lib/api";
import { btnPrimary, btnSecondary, card, error as errorCls, input, label } from "@/lib/styles";
import type { Plan } from "@/lib/types";

interface Props {
  orgId: number;
  currentPlanSlug: string;
  onClose: () => void;
  onChanged: () => void;
}

export default function ChangePlanModal({ orgId, currentPlanSlug, onClose, onChanged }: Props) {
  const [plans, setPlans] = useState<Plan[]>([]);
  const [planId, setPlanId] = useState<number | "">("");
  const [errorMsg, setErrorMsg] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    apiFetch<Plan[]>("/api/v1/plans/all").then((all) => {
      setPlans(all);
      const current = all.find((p) => p.slug === currentPlanSlug);
      if (current) setPlanId(current.id);
    });
  }, [currentPlanSlug]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (planId === "") return;
    setSubmitting(true);
    setErrorMsg("");
    try {
      // Existing L4.3 subscription override endpoint accepts plan_id.
      await apiFetch(`/api/v1/admin/orgs/${orgId}/subscription`, {
        method: "PUT",
        body: JSON.stringify({ plan_id: planId }),
      });
      onChanged();
      onClose();
    } catch (err) {
      setErrorMsg(extractErrorMessage(err, "Failed to change plan"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <form onSubmit={handleSubmit} className={`${card} w-full max-w-md p-6`}>
        <h2 className="mb-4 text-lg font-semibold">Change plan</h2>
        {errorMsg && <div className={`${errorCls} mb-3`}>{errorMsg}</div>}
        <label className={label}>Plan</label>
        <select
          value={planId}
          onChange={(e) => setPlanId(e.target.value === "" ? "" : Number(e.target.value))}
          className={input}
        >
          <option value="">Select plan...</option>
          {plans.map((p) => (
            <option key={p.id} value={p.id}>{p.name} ({p.slug})</option>
          ))}
        </select>
        <div className="mt-4 flex justify-end gap-2">
          <button type="button" onClick={onClose} className={btnSecondary}>Cancel</button>
          <button type="submit" disabled={submitting || planId === ""} className={btnPrimary}>
            {submitting ? "Saving..." : "Save"}
          </button>
        </div>
      </form>
    </div>
  );
}
