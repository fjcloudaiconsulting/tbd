"use client";

import { useEffect, useRef, useState } from "react";

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

  const dialogRef = useRef<HTMLFormElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    apiFetch<Plan[]>("/api/v1/plans/all").then((all) => {
      setPlans(all);
      const current = all.find((p) => p.slug === currentPlanSlug);
      if (current) setPlanId(current.id);
    });
  }, [currentPlanSlug]);

  useEffect(() => {
    previousFocusRef.current = document.activeElement as HTMLElement;
    const focusable = dialogRef.current?.querySelector<HTMLElement>(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
    );
    focusable?.focus();
    return () => {
      previousFocusRef.current?.focus();
      previousFocusRef.current = null;
    };
  }, []);

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); onClose(); return; }
      if (e.key === "Tab") {
        const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        );
        if (!focusable || focusable.length === 0) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault(); last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault(); first.focus();
        }
      }
    };
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [onClose]);

  useEffect(() => {
    document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = ""; };
  }, []);

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
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-bg/80 p-4"
      onClick={onClose}
    >
      <form
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="change-plan-modal-title"
        onSubmit={handleSubmit}
        onClick={(e) => e.stopPropagation()}
        className={`${card} w-full max-w-[min(28rem,calc(100vw-2rem))] p-6`}
      >
        <h2 id="change-plan-modal-title" className="mb-4 text-lg font-semibold">Change plan</h2>
        {errorMsg && <div className={`${errorCls} mb-3`}>{errorMsg}</div>}
        <label htmlFor="change-plan-select" className={label}>Plan</label>
        <select
          id="change-plan-select"
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
