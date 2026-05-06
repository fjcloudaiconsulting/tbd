"use client";

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

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
  masterCategories: Category[];
  onCreated: (cat: Category) => void;
  onCancel: () => void;
}

export default function AddCategoryModal({
  initialName,
  initialType,
  masterCategories,
  onCreated,
  onCancel,
}: Props) {
  const [name, setName] = useState(initialName);
  const [type, setType] = useState<"income" | "expense" | "both">(initialType);
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
    setMounted(true);
  }, []);

  // Focus + restore
  useEffect(() => {
    previousFocusRef.current = document.activeElement as HTMLElement;
    nameRef.current?.focus();
    nameRef.current?.select();
    return () => {
      previousFocusRef.current?.focus();
    };
  }, []);

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
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/50 p-4">
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

          <fieldset>
            <legend className={labelCls}>Type</legend>
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
                {masterCategories.map((c) => (
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
