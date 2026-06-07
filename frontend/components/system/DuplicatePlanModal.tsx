"use client";

import { useEffect, useRef, useState } from "react";

import { apiFetch, extractErrorMessage } from "@/lib/api";
import { useFocusTrap } from "@/lib/hooks/use-focus-trap";
import { btnPrimary, btnSecondary, card, error as errorCls, input, label } from "@/lib/styles";
import type { Plan } from "@/lib/types";

interface Props {
  source: Plan;
  onClose: () => void;
  onDuplicated: (plan: Plan) => void;
}

// Local slug helper. No other call site needs this; intentionally not promoted to a shared lib.
function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/-{2,}/g, "-")
    .replace(/^-+|-+$/g, "");
}

export default function DuplicatePlanModal({ source, onClose, onDuplicated }: Props) {
  const [name, setName] = useState(`${source.name} (copy)`);
  const [slug, setSlug] = useState(slugify(`${source.name}-copy`));
  const [errorMsg, setErrorMsg] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const dialogRef = useRef<HTMLFormElement>(null);

  useFocusTrap({ active: true, containerRef: dialogRef });

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); onClose(); }
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
    setErrorMsg("");
    setSubmitting(true);
    try {
      const plan = await apiFetch<Plan>(`/api/v1/plans/${source.id}/duplicate`, {
        method: "POST",
        body: JSON.stringify({ name, slug }),
      });
      onDuplicated(plan);
      onClose();
    } catch (err) {
      setErrorMsg(extractErrorMessage(err, "Failed to duplicate plan"));
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
        aria-labelledby="duplicate-plan-modal-title"
        onSubmit={handleSubmit}
        onClick={(e) => e.stopPropagation()}
        className={`${card} w-full max-w-md p-6`}
      >
        <h2 id="duplicate-plan-modal-title" className="mb-4 text-lg font-semibold">Duplicate plan</h2>
        {errorMsg && <div className={`${errorCls} mb-3`}>{errorMsg}</div>}
        <div className="mb-3">
          <label htmlFor="duplicate-plan-name" className={label}>Name</label>
          <input
            id="duplicate-plan-name"
            type="text"
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              setSlug(slugify(e.target.value));
            }}
            className={input}
            required
          />
        </div>
        <div className="mb-4">
          <label htmlFor="duplicate-plan-slug" className={label}>Slug</label>
          <input
            id="duplicate-plan-slug"
            type="text"
            value={slug}
            onChange={(e) => setSlug(e.target.value)}
            className={input}
            pattern={"^[a-z0-9\\-]+$"}
            required
          />
        </div>
        <div className="flex justify-end gap-2">
          <button type="button" onClick={onClose} className={btnSecondary}>Cancel</button>
          <button type="submit" disabled={submitting} className={btnPrimary}>
            {submitting ? "Duplicating..." : "Duplicate"}
          </button>
        </div>
      </form>
    </div>
  );
}
