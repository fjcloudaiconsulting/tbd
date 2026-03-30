"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import AppShell from "@/components/AppShell";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import { input, btnPrimary, card, cardHeader, cardTitle, error as errorCls, pageTitle } from "@/lib/styles";
import type { Category } from "@/lib/types";

export default function CategoriesPage() {
  const { user, loading } = useAuth();
  const [categories, setCategories] = useState<Category[]>([]);
  const [fetching, setFetching] = useState(true);
  const [name, setName] = useState("");
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editingName, setEditingName] = useState("");
  const [error, setError] = useState("");

  const reload = useCallback(async () => {
    const data = await apiFetch<Category[]>("/api/v1/categories");
    setCategories(data ?? []);
    setFetching(false);
  }, []);

  useEffect(() => {
    if (!loading && user) reload().catch(() => setFetching(false));
  }, [loading, user, reload]);

  async function handleAdd(e: FormEvent) {
    e.preventDefault();
    setError("");
    try {
      await apiFetch("/api/v1/categories", { method: "POST", body: JSON.stringify({ name }) });
      setName("");
      await reload();
    } catch (err) { setError(err instanceof Error ? err.message : "Failed"); }
  }

  async function handleUpdate(id: number) {
    setError("");
    try {
      await apiFetch(`/api/v1/categories/${id}`, { method: "PUT", body: JSON.stringify({ name: editingName }) });
      setEditingId(null);
      await reload();
    } catch (err) { setError(err instanceof Error ? err.message : "Failed"); }
  }

  async function handleDelete(id: number) {
    if (!confirm("Delete this category?")) return;
    setError("");
    try {
      await apiFetch(`/api/v1/categories/${id}`, { method: "DELETE" });
      await reload();
    } catch (err) { setError(err instanceof Error ? err.message : "Failed"); }
  }

  return (
    <AppShell>
      <h1 className={pageTitle}>Categories</h1>

      {error && <div className={`mb-6 ${errorCls}`}>{error}</div>}

      {fetching ? (
        <Spinner />
      ) : (
        <div className={`max-w-lg ${card}`}>
          <div className={cardHeader}>
            <h2 className={cardTitle}>Manage Categories</h2>
          </div>
          <div className="p-6">
            <form onSubmit={handleAdd} className="mb-5 flex gap-2">
              <div className="flex-1">
                <label htmlFor="cat-name" className="sr-only">New category name</label>
                <input id="cat-name" type="text" required placeholder="New category name" value={name} onChange={(e) => setName(e.target.value)} className={input} />
              </div>
              <button type="submit" className={btnPrimary}>Add</button>
            </form>
            <div className="space-y-1">
              {categories.map((cat) => (
                <div key={cat.id} className="flex items-center justify-between rounded-md px-3 py-2.5 transition-colors hover:bg-surface-raised">
                  {editingId === cat.id ? (
                    <div className="flex flex-1 gap-2">
                      <label htmlFor={`edit-cat-${cat.id}`} className="sr-only">Edit category name</label>
                      <input id={`edit-cat-${cat.id}`} type="text" value={editingName} onChange={(e) => setEditingName(e.target.value)} className={`flex-1 ${input}`} autoFocus
                        onKeyDown={(e) => { if (e.key === "Enter") handleUpdate(cat.id); if (e.key === "Escape") setEditingId(null); }} />
                      <button onClick={() => handleUpdate(cat.id)} className="text-sm text-accent hover:text-accent-hover">Save</button>
                      <button onClick={() => setEditingId(null)} className="text-sm text-text-muted hover:text-text-secondary">Cancel</button>
                    </div>
                  ) : (
                    <>
                      <div className="flex items-center gap-2">
                        <span className="text-sm text-text-primary">{cat.name}</span>
                        <span className="text-xs text-text-muted">{cat.transaction_count}</span>
                      </div>
                      <div className="flex gap-3">
                        <button onClick={() => { setEditingId(cat.id); setEditingName(cat.name); }} aria-label={`Edit ${cat.name}`} className="text-xs text-text-muted hover:text-accent">Edit</button>
                        <button onClick={() => handleDelete(cat.id)} aria-label={`Delete ${cat.name}`} className="text-xs text-text-muted hover:text-danger">Delete</button>
                      </div>
                    </>
                  )}
                </div>
              ))}
              {categories.length === 0 && <p className="py-4 text-center text-sm text-text-muted">No categories yet. Add one above.</p>}
            </div>
          </div>
        </div>
      )}
    </AppShell>
  );
}
