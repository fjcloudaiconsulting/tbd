"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import type { Category } from "@/lib/types";

export default function CategoriesPage() {
  const { user, loading } = useAuth();
  const [categories, setCategories] = useState<Category[]>([]);
  const [name, setName] = useState("");
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editingName, setEditingName] = useState("");
  const [error, setError] = useState("");

  const reload = useCallback(async () => {
    const data = await apiFetch<Category[]>("/api/v1/categories");
    setCategories(data ?? []);
  }, []);

  useEffect(() => {
    if (!loading && user) reload().catch(() => {});
  }, [loading, user, reload]);

  async function handleAdd(e: FormEvent) {
    e.preventDefault();
    setError("");
    try {
      await apiFetch("/api/v1/categories", {
        method: "POST",
        body: JSON.stringify({ name }),
      });
      setName("");
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    }
  }

  async function handleUpdate(id: number) {
    setError("");
    try {
      await apiFetch(`/api/v1/categories/${id}`, {
        method: "PUT",
        body: JSON.stringify({ name: editingName }),
      });
      setEditingId(null);
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    }
  }

  async function handleDelete(id: number) {
    if (!confirm("Delete this category?")) return;
    setError("");
    try {
      await apiFetch(`/api/v1/categories/${id}`, { method: "DELETE" });
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    }
  }

  const inputClass =
    "w-full rounded-md border border-border bg-surface-raised px-3 py-2 text-sm text-text-primary placeholder:text-text-muted focus:border-accent focus:outline-none";

  return (
    <AppShell>
      <h1 className="mb-8 font-display text-2xl text-text-primary">Categories</h1>

      {error && (
        <div className="mb-6 rounded-md bg-danger-dim px-4 py-3 text-sm text-danger">
          {error}
        </div>
      )}

      <div className="max-w-lg rounded-lg border border-border bg-surface">
        <div className="border-b border-border px-6 py-4">
          <h2 className="text-xs font-medium uppercase tracking-wider text-text-muted">
            Manage Categories
          </h2>
        </div>
        <div className="p-6">
          <form onSubmit={handleAdd} className="mb-5 flex gap-2">
            <input
              type="text"
              required
              placeholder="New category name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className={`flex-1 ${inputClass}`}
            />
            <button
              type="submit"
              className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-accent-text hover:bg-accent-hover"
            >
              Add
            </button>
          </form>
          <div className="space-y-1">
            {categories.map((cat) => (
              <div
                key={cat.id}
                className="flex items-center justify-between rounded-md px-3 py-2.5 transition-colors hover:bg-surface-raised"
              >
                {editingId === cat.id ? (
                  <div className="flex flex-1 gap-2">
                    <input
                      type="text"
                      value={editingName}
                      onChange={(e) => setEditingName(e.target.value)}
                      className={`flex-1 ${inputClass}`}
                      autoFocus
                      onKeyDown={(e) => {
                        if (e.key === "Enter") handleUpdate(cat.id);
                        if (e.key === "Escape") setEditingId(null);
                      }}
                    />
                    <button
                      onClick={() => handleUpdate(cat.id)}
                      className="text-sm text-accent hover:text-accent-hover"
                    >
                      Save
                    </button>
                    <button
                      onClick={() => setEditingId(null)}
                      className="text-sm text-text-muted hover:text-text-secondary"
                    >
                      Cancel
                    </button>
                  </div>
                ) : (
                  <>
                    <div className="flex items-center gap-2">
                      <span className="text-sm text-text-primary">{cat.name}</span>
                      <span className="text-xs text-text-muted">
                        {cat.transaction_count}
                      </span>
                    </div>
                    <div className="flex gap-3">
                      <button
                        onClick={() => {
                          setEditingId(cat.id);
                          setEditingName(cat.name);
                        }}
                        className="text-xs text-text-muted hover:text-accent"
                      >
                        Edit
                      </button>
                      <button
                        onClick={() => handleDelete(cat.id)}
                        className="text-xs text-text-muted hover:text-danger"
                      >
                        Delete
                      </button>
                    </div>
                  </>
                )}
              </div>
            ))}
            {categories.length === 0 && (
              <p className="py-4 text-center text-sm text-text-muted">
                No categories yet. Add one above.
              </p>
            )}
          </div>
        </div>
      </div>
    </AppShell>
  );
}
