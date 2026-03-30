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
      setSettings(data);
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

  if (!isAdmin) return null;

  return (
    <AppShell>
      <h1 className="mb-6 text-xl font-semibold">Organization Settings</h1>

      <div className="max-w-2xl space-y-6">
        {/* Org info */}
        <div className="rounded-lg border border-gray-200 bg-white p-5">
          <h2 className="mb-2 text-sm font-medium text-gray-700">
            Organization
          </h2>
          <p className="text-sm">{user?.org_name}</p>
        </div>

        {/* Settings key-value editor */}
        <div className="rounded-lg border border-gray-200 bg-white">
          <div className="border-b border-gray-100 px-5 py-3">
            <h2 className="text-sm font-medium text-gray-700">
              Configuration
            </h2>
            <p className="mt-0.5 text-xs text-gray-400">
              Runtime settings persisted in the database. Use this for SSO, AI
              providers, and other org-level configuration.
            </p>
          </div>
          <div className="p-5">
            {error && (
              <div className="mb-4 rounded bg-red-50 p-2 text-sm text-red-700">
                {error}
              </div>
            )}

            <form onSubmit={handleAdd} className="mb-4 flex gap-2">
              <input
                type="text"
                required
                placeholder="Key"
                value={key}
                onChange={(e) => setKey(e.target.value)}
                className="w-40 rounded border border-gray-300 px-3 py-1.5 text-sm focus:border-blue-500 focus:outline-none"
              />
              <input
                type="text"
                required
                placeholder="Value"
                value={value}
                onChange={(e) => setValue(e.target.value)}
                className="flex-1 rounded border border-gray-300 px-3 py-1.5 text-sm focus:border-blue-500 focus:outline-none"
              />
              <button
                type="submit"
                className="rounded bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700"
              >
                Add
              </button>
            </form>

            <div className="space-y-2">
              {settings.map((s) => (
                <div
                  key={s.key}
                  className="flex items-center justify-between rounded border border-gray-100 px-3 py-2"
                >
                  {editingKey === s.key ? (
                    <div className="flex flex-1 gap-2">
                      <span className="w-40 py-1 text-sm font-medium text-gray-600">
                        {s.key}
                      </span>
                      <input
                        type="text"
                        value={editingValue}
                        onChange={(e) => setEditingValue(e.target.value)}
                        className="flex-1 rounded border border-gray-300 px-2 py-1 text-sm focus:border-blue-500 focus:outline-none"
                        autoFocus
                        onKeyDown={(e) => {
                          if (e.key === "Enter") handleUpdate(s.key);
                          if (e.key === "Escape") setEditingKey(null);
                        }}
                      />
                      <button
                        onClick={() => handleUpdate(s.key)}
                        className="text-sm text-blue-600 hover:underline"
                      >
                        Save
                      </button>
                      <button
                        onClick={() => setEditingKey(null)}
                        className="text-sm text-gray-400 hover:underline"
                      >
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <>
                      <div>
                        <span className="text-sm font-medium text-gray-600">
                          {s.key}
                        </span>
                        <span className="ml-3 text-sm text-gray-500">
                          {s.value}
                        </span>
                      </div>
                      <div className="flex gap-2">
                        <button
                          onClick={() => {
                            setEditingKey(s.key);
                            setEditingValue(s.value);
                          }}
                          className="text-xs text-blue-600 hover:underline"
                        >
                          Edit
                        </button>
                        <button
                          onClick={() => handleDelete(s.key)}
                          className="text-xs text-red-500 hover:underline"
                        >
                          Delete
                        </button>
                      </div>
                    </>
                  )}
                </div>
              ))}
              {settings.length === 0 && (
                <p className="text-sm text-gray-400">
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
