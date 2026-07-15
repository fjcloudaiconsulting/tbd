"use client";

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import HelpTooltip from "@/components/help/HelpTooltip";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import {
  btnPrimary,
  btnSecondary,
  card,
  error as errorCls,
  input,
  label as labelCls,
} from "@/lib/styles";
import type { Category } from "@/lib/types";

interface Props {
  initialName: string;
  initialType: "income" | "expense" | "both";
  /**
   * When provided, the form is locked to this type:
   * the user cannot change it, and the parent-master dropdown
   * (when "Subcategory" is checked) only lists masters whose type
   * matches exactly (no "both" parents). Child categories inherit
   * parent type at the backend, so allowing a "both" parent would
   * silently create a "both" child against the modal's type-only
   * promise. The submitted POST always carries the locked type.
   * The backend rejects any (parent.type, child.type) mismatch,
   * so we mirror that here to keep the user from hitting a 400
   * mid-flow.
   */
  lockedType?: "income" | "expense";
  masterCategories: Category[];
  onCreated: (cat: Category) => void;
  onCancel: () => void;
}

export default function AddCategoryModal({
  initialName,
  initialType,
  lockedType,
  masterCategories,
  onCreated,
  onCancel,
}: Props) {
  const [name, setName] = useState(initialName);
  // When lockedType is set, the form's type follows it regardless of
  // what initialType was passed (the lock wins).
  const [type, setType] = useState<"income" | "expense" | "both">(
    lockedType ?? initialType,
  );
  const [isSub, setIsSub] = useState(false);
  const [parentId, setParentId] = useState<number | "">("");
  const [description, setDescription] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [errorText, setErrorText] = useState<string | null>(null);
  const [mounted, setMounted] = useState(false);

  const dialogRef = useRef<HTMLDivElement>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- flag mounted after first commit so the portal + focus effect run client-side only (avoids SSR hydration mismatch)
    setMounted(true);
  }, []);

  // Focus + restore. Gated on `mounted` so it runs after the portal
  // content actually renders. Without the gate, nameRef.current is
  // null on first run and keyboard focus stays behind the aria-modal
  // dialog.
  useEffect(() => {
    if (!mounted) return;
    previousFocusRef.current = document.activeElement as HTMLElement;
    nameRef.current?.focus();
    nameRef.current?.select();
    return () => {
      previousFocusRef.current?.focus();
    };
  }, [mounted]);

  // Escape + focus trap
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !submitting) {
        e.stopPropagation();
        onCancel();
        return;
      }
      if (e.key === "Tab") {
        const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        );
        if (!focusable || focusable.length === 0) return;
        const visible = Array.from(focusable).filter(
          (el) => !el.hasAttribute("disabled") && el.offsetParent !== null
        );
        if (visible.length === 0) return;
        const first = visible[0];
        const last = visible[visible.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [submitting, onCancel]);

  const trimmedName = name.trim();
  const needsParent = isSub && parentId === "";
  const canSubmit =
    trimmedName.length > 0 &&
    trimmedName.length <= 100 &&
    !submitting &&
    !needsParent;

  async function handleSubmit() {
    if (!canSubmit) return;
    setSubmitting(true);
    setErrorText(null);
    try {
      const body: Record<string, unknown> = {
        name: trimmedName,
        type,
      };
      if (isSub && parentId !== "") {
        body.parent_id = parentId;
      }
      const trimmedDescription = description.trim();
      if (trimmedDescription) {
        body.description = trimmedDescription;
      }
      const created = await apiFetch<Category>("/api/v1/categories", {
        method: "POST",
        body: JSON.stringify(body),
      });
      onCreated(created);
    } catch (err) {
      setErrorText(extractErrorMessage(err, "Failed to create category"));
    } finally {
      setSubmitting(false);
    }
  }

  if (!mounted) return null;

  const modal = (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-bg/80 p-4">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="add-category-title"
        className={`${card} w-full max-w-md p-6 shadow-xl`}
      >
        <h2
          id="add-category-title"
          className="mb-4 text-lg font-semibold text-text-primary"
        >
          New category
        </h2>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            // React synthetic events bubble through the React tree,
            // not the DOM tree, so ``createPortal`` does NOT isolate
            // this submit from a parent <form> in the React tree
            // (e.g. the floating Add Transaction form). Without
            // ``stopPropagation`` the parent form's onSubmit fires
            // with stale/empty state and returns 422 from the
            // backend. See add-category-modal.test.tsx → "does NOT
            // bubble its submit event into a parent form".
            e.stopPropagation();
            handleSubmit();
          }}
          className="space-y-4"
        >
          <div>
            <label htmlFor="add-cat-name" className={labelCls}>
              Name
            </label>
            <input
              ref={nameRef}
              id="add-cat-name"
              type="text"
              required
              maxLength={100}
              value={name}
              onChange={(e) => setName(e.target.value)}
              className={input}
              autoComplete="off"
            />
          </div>

          {lockedType ? (
            <div>
              <span className={labelCls}>Type</span>
              <p className="text-sm capitalize text-text-muted">
                {lockedType} only
              </p>
            </div>
          ) : (
            <fieldset>
              <legend className="mb-1.5 flex items-center gap-1">
                <span className={`${labelCls} mb-0`}>Type</span>
                <HelpTooltip k="cat.type" />
              </legend>
              <div className="flex gap-4 text-sm text-text-primary">
                {(["expense", "income", "both"] as const).map((t) => (
                  <label key={t} className="flex items-center gap-1.5">
                    <input
                      type="radio"
                      name="add-cat-type"
                      value={t}
                      checked={type === t}
                      onChange={() => setType(t)}
                    />
                    <span className="capitalize">{t}</span>
                  </label>
                ))}
              </div>
            </fieldset>
          )}

          <div className="flex items-center gap-2 text-sm text-text-primary">
            <input
              id="add-cat-issub"
              type="checkbox"
              checked={isSub}
              onChange={(e) => {
                setIsSub(e.target.checked);
                if (!e.target.checked) setParentId("");
              }}
            />
            <label htmlFor="add-cat-issub">Subcategory</label>
            <HelpTooltip k="cat.subcategory" />
          </div>

          {isSub && (
            <div>
              <label htmlFor="add-cat-parent" className={labelCls}>
                Parent category
              </label>
              <select
                id="add-cat-parent"
                value={parentId === "" ? "" : String(parentId)}
                onChange={(e) =>
                  setParentId(
                    e.target.value === "" ? "" : Number(e.target.value)
                  )
                }
                className={input}
                aria-describedby={
                  needsParent ? "add-cat-parent-help" : undefined
                }
                aria-invalid={needsParent || undefined}
              >
                <option value="">Select a parent...</option>
                {masterCategories
                  .filter((c) => !lockedType || c.type === lockedType)
                  .map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
              </select>
              {needsParent && (
                <p
                  id="add-cat-parent-help"
                  className="mt-1 text-xs text-text-muted"
                >
                  Pick a parent category
                </p>
              )}
            </div>
          )}

          <div>
            <label htmlFor="add-cat-desc" className={labelCls}>
              Description (optional)
            </label>
            <input
              id="add-cat-desc"
              type="text"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className={input}
              autoComplete="off"
            />
          </div>

          {errorText && (
            <div role="alert" className={errorCls}>
              {errorText}
            </div>
          )}

          <div className="flex justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={onCancel}
              disabled={submitting}
              className={btnSecondary}
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!canSubmit}
              className={btnPrimary}
            >
              {submitting ? "Adding..." : "Add category"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );

  return createPortal(modal, document.body);
}
