"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import type { OrgSetting } from "@/lib/types";

export default function SettingsPage() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const [settings, setSettings] = useState<OrgSetting[]>([]);
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editingValue, setEditingValue] = useState("");

  const isAdmin =
    user?.role === "owner" || user?.role === "admin" || user?.is_superadmin;

  useEffect(() => {
    if (!loading && !isAdmin) {
      router.replace("/dashboard");
    }
  }, [loading, isAdmin, router]);

  const reload = useCallback(async () => {
    try {
      const data = await apiFetch<OrgSetting[]>("/api/v1/settings");
      setSettings(data ?? []);
    } catch {
      // May 403 if not admin
    }
  }, []);

  useEffect(() => {
    if (isAdmin) reload();
  }, [isAdmin, reload]);

  async function handleAdd(e: FormEvent) {
    e.preventDefault();
    setError("");
    try {
      await apiFetch("/api/v1/settings", {
        method: "PUT",
        body: JSON.stringify({ key, value }),
      });
      setKey("");
      setValue("");
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    }
  }

  async function handleUpdate(settingKey: string) {
    setError("");
    try {
      await apiFetch("/api/v1/settings", {
        method: "PUT",
        body: JSON.stringify({ key: settingKey, value: editingValue }),
      });
      setEditingKey(null);
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    }
  }

  async function handleDelete(settingKey: string) {
    if (!confirm(`Delete setting "${settingKey}"?`)) return;
    setError("");
    try {
      await apiFetch(`/api/v1/settings/${encodeURIComponent(settingKey)}`, {
        method: "DELETE",
      });
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    }
  }

  if (loading || !isAdmin) {
    return (
      <AppShell>
        {loading && (
          <div className="flex justify-center py-12">
            <div className="h-6 w-6 animate-spin rounded-full border-2 border-border border-t-accent" />
          </div>
        )}
      </AppShell>
    );
  }

  const inputClass =
    "rounded-md border border-border bg-surface-raised px-3 py-2 text-sm text-text-primary placeholder:text-text-muted focus:border-accent focus:outline-none";

  return (
    <AppShell>
      <h1 className="mb-8 font-display text-2xl text-text-primary">
        Organization Settings
      </h1>

      <div className="max-w-2xl space-y-6">
        {/* Org info */}
        <div className="rounded-lg border border-border bg-surface p-6">
          <h2 className="mb-2 text-xs font-medium uppercase tracking-wider text-text-muted">
            Organization
          </h2>
          <p className="text-sm text-text-primary">{user?.org_name}</p>
        </div>

        {/* Settings key-value editor */}
        <div className="rounded-lg border border-border bg-surface">
          <div className="border-b border-border px-6 py-4">
            <h2 className="text-xs font-medium uppercase tracking-wider text-text-muted">
              Configuration
            </h2>
            <p className="mt-1 text-xs text-text-muted">
              Runtime settings persisted in the database.
            </p>
          </div>
          <div className="p-6">
            {error && (
              <div className="mb-5 rounded-md bg-danger-dim px-4 py-3 text-sm text-danger">
                {error}
              </div>
            )}

            <form onSubmit={handleAdd} className="mb-5 flex gap-2">
              <input
                type="text"
                required
                placeholder="Key"
                value={key}
                onChange={(e) => setKey(e.target.value)}
                className={`w-40 ${inputClass}`}
              />
              <input
                type="text"
                required
                placeholder="Value"
                value={value}
                onChange={(e) => setValue(e.target.value)}
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
              {settings.map((s) => (
                <div
                  key={s.key}
                  className="flex items-center justify-between rounded-md px-3 py-2.5 transition-colors hover:bg-surface-raised"
                >
                  {editingKey === s.key ? (
                    <div className="flex flex-1 gap-2">
                      <span className="w-40 py-1 text-sm font-medium text-text-secondary">
                        {s.key}
                      </span>
                      <input
                        type="text"
                        value={editingValue}
                        onChange={(e) => setEditingValue(e.target.value)}
                        className={`flex-1 ${inputClass}`}
                        autoFocus
                        onKeyDown={(e) => {
                          if (e.key === "Enter") handleUpdate(s.key);
                          if (e.key === "Escape") setEditingKey(null);
                        }}
                      />
                      <button
                        onClick={() => handleUpdate(s.key)}
                        className="text-sm text-accent hover:text-accent-hover"
                      >
                        Save
                      </button>
                      <button
                        onClick={() => setEditingKey(null)}
                        className="text-sm text-text-muted hover:text-text-secondary"
                      >
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <>
                      <div>
                        <span className="text-sm font-medium text-text-secondary">
                          {s.key}
                        </span>
                        <span className="ml-3 text-sm text-text-muted">
                          {s.value}
                        </span>
                      </div>
                      <div className="flex gap-3">
                        <button
                          onClick={() => {
                            setEditingKey(s.key);
                            setEditingValue(s.value);
                          }}
                          className="text-xs text-text-muted hover:text-accent"
                        >
                          Edit
                        </button>
                        <button
                          onClick={() => handleDelete(s.key)}
                          className="text-xs text-text-muted hover:text-danger"
                        >
                          Delete
                        </button>
                      </div>
                    </>
                  )}
                </div>
              ))}
              {settings.length === 0 && (
                <p className="py-4 text-center text-sm text-text-muted">
                  No settings configured yet.
                </p>
              )}
            </div>
          </div>
        </div>
      </div>
    </AppShell>
  );
}
