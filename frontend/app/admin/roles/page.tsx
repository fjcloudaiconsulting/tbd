"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import AppShell from "@/components/AppShell";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { isSuperadmin } from "@/lib/auth";
import {
  btnPrimary,
  btnSecondary,
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
  input,
  label as labelCls,
  pageTitle,
} from "@/lib/styles";
import type {
  PermissionCatalogResponse,
  RoleCreatePayload,
  RoleDetail,
  RoleListItem,
  RoleListResponse,
} from "@/lib/types";

const SLUG_PATTERN = /^[a-z][a-z0-9_]{2,63}$/;

interface CreateModalProps {
  catalog: PermissionCatalogResponse;
  onClose: () => void;
  onCreated: () => void;
}

function CreateRoleModal({ catalog, onClose, onCreated }: CreateModalProps) {
  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState("");

  const slugValid = SLUG_PATTERN.test(slug);

  function togglePermission(key: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr("");
    if (!slugValid) {
      setErr(
        "Slug must start with a lowercase letter and contain only lowercase letters, digits, and underscores (3 to 64 chars).",
      );
      return;
    }
    if (!name.trim()) {
      setErr("Name is required.");
      return;
    }
    setSubmitting(true);
    try {
      const payload: RoleCreatePayload = {
        slug,
        name: name.trim(),
        description: description.trim() ? description.trim() : null,
        permissions: Array.from(selected).sort(),
      };
      await apiFetch<RoleDetail>("/api/v1/admin/roles", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      onCreated();
    } catch (e) {
      setErr(extractErrorMessage(e, "Failed to create role"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="create-role-title"
    >
      <div className={`${card} w-full max-w-2xl max-h-[90vh] overflow-y-auto`}>
        <div className={cardHeader}>
          <h2 id="create-role-title" className={cardTitle}>
            New role
          </h2>
        </div>
        <form onSubmit={submit} className="space-y-4 px-6 py-4">
          {err && (
            <div className={errorCls} role="alert">
              {err}
            </div>
          )}
          <div>
            <label htmlFor="role-slug" className={labelCls}>
              Slug
            </label>
            <input
              id="role-slug"
              type="text"
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              className={input}
              placeholder="support"
              autoComplete="off"
              required
            />
            <p className="mt-1 text-xs text-text-muted">
              Lowercase letters, digits, underscores. Must start with a letter.
            </p>
          </div>
          <div>
            <label htmlFor="role-name" className={labelCls}>
              Name
            </label>
            <input
              id="role-name"
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className={input}
              placeholder="Support"
              maxLength={120}
              required
            />
          </div>
          <div>
            <label htmlFor="role-description" className={labelCls}>
              Description (optional)
            </label>
            <textarea
              id="role-description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className={`${input} min-h-[5rem]`}
              maxLength={500}
              placeholder="What this role can do"
            />
          </div>
          <div>
            <p className={`${labelCls} mb-2`}>Permissions</p>
            <div className="space-y-3">
              {Object.entries(catalog.namespaces).map(([ns, keys]) => (
                <fieldset
                  key={ns}
                  className="rounded-md border border-border-subtle px-3 py-2"
                >
                  <legend className="px-1 text-xs font-semibold uppercase tracking-wider text-text-muted">
                    {ns}
                  </legend>
                  <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
                    {keys.map((key) => (
                      <label
                        key={key}
                        className="flex items-center gap-2 rounded px-1 py-1 text-sm text-text-primary hover:bg-surface-raised"
                      >
                        <input
                          type="checkbox"
                          checked={selected.has(key)}
                          onChange={() => togglePermission(key)}
                          className="h-4 w-4 rounded border-border accent-accent"
                        />
                        <span className="font-mono text-xs">{key}</span>
                      </label>
                    ))}
                  </div>
                </fieldset>
              ))}
            </div>
          </div>
          <div className="flex items-center justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={onClose}
              className={btnSecondary}
              disabled={submitting}
            >
              Cancel
            </button>
            <button
              type="submit"
              className={btnPrimary}
              disabled={submitting || !slugValid || !name.trim()}
            >
              {submitting ? "Creating…" : "Create role"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default function AdminRolesPage() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const [data, setData] = useState<RoleListResponse | null>(null);
  const [catalog, setCatalog] = useState<PermissionCatalogResponse | null>(null);
  const [error, setError] = useState("");
  const [fetching, setFetching] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [reloadCounter, setReloadCounter] = useState(0);

  useEffect(() => {
    if (loading) return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (!isSuperadmin(user)) {
      router.replace("/dashboard");
    }
  }, [loading, user, router]);

  useEffect(() => {
    if (loading || !user || !isSuperadmin(user)) return;
    setFetching(true);
    Promise.all([
      apiFetch<RoleListResponse>("/api/v1/admin/roles"),
      apiFetch<PermissionCatalogResponse>("/api/v1/admin/permissions"),
    ])
      .then(([roles, perms]) => {
        setData(roles);
        setCatalog(perms);
      })
      .catch((err) => setError(extractErrorMessage(err, "Failed to load")))
      .finally(() => setFetching(false));
  }, [loading, user, reloadCounter]);

  const sortedItems: RoleListItem[] = useMemo(() => {
    if (!data) return [];
    return [...data.items].sort((a, b) => {
      // Frozen first, then by name.
      if (a.is_system_frozen !== b.is_system_frozen) {
        return a.is_system_frozen ? -1 : 1;
      }
      return a.name.localeCompare(b.name);
    });
  }, [data]);

  if (loading || !user || !isSuperadmin(user)) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner />
      </div>
    );
  }

  return (
    <AppShell>
      <div className="mb-8 flex items-end justify-between gap-4">
        <h1 className={`${pageTitle} mb-0`}>Roles</h1>
        <button
          type="button"
          className={btnPrimary}
          onClick={() => setShowCreate(true)}
          disabled={!catalog}
        >
          + New role
        </button>
      </div>

      {error && (
        <div className={`${errorCls} mb-4`} role="alert">
          {error}
        </div>
      )}

      <div className={`${card} mb-6`}>
        <div className={cardHeader}>
          <h2 className={cardTitle}>Platform roles</h2>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-y border-border text-left text-xs uppercase tracking-wider text-text-muted">
                <th className="px-6 py-3">Name</th>
                <th className="px-6 py-3">Slug</th>
                <th className="px-6 py-3">Permissions</th>
                <th className="px-6 py-3">Type</th>
                <th className="px-6 py-3 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {fetching && (
                <tr>
                  <td
                    colSpan={5}
                    className="px-6 py-6 text-center text-text-muted"
                  >
                    Loading…
                  </td>
                </tr>
              )}
              {!fetching && sortedItems.length === 0 && (
                <tr>
                  <td
                    colSpan={5}
                    className="px-6 py-6 text-center text-text-muted"
                  >
                    No roles defined.
                  </td>
                </tr>
              )}
              {!fetching &&
                sortedItems.map((row) => (
                  <tr
                    key={row.id}
                    className="border-b border-border-subtle"
                  >
                    <td className="px-6 py-3">
                      <Link
                        href={`/admin/roles/${row.id}`}
                        className="text-accent hover:text-accent-hover"
                      >
                        {row.name}
                      </Link>
                      {row.description && (
                        <p className="mt-0.5 text-xs text-text-muted">
                          {row.description}
                        </p>
                      )}
                    </td>
                    <td className="px-6 py-3 font-mono text-xs text-text-secondary">
                      {row.slug}
                    </td>
                    <td className="px-6 py-3 text-text-secondary tabular-nums">
                      {row.permission_count}
                    </td>
                    <td className="px-6 py-3">
                      {row.is_system_frozen ? (
                        <span className="rounded-full bg-accent/10 px-2 py-0.5 text-xs font-semibold uppercase tracking-wider text-accent">
                          system
                        </span>
                      ) : (
                        <span className="text-xs text-text-muted">custom</span>
                      )}
                    </td>
                    <td className="px-6 py-3 text-right">
                      <Link
                        href={`/admin/roles/${row.id}`}
                        className="text-xs text-text-muted hover:text-accent"
                      >
                        View
                      </Link>
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      </div>

      {showCreate && catalog && (
        <CreateRoleModal
          catalog={catalog}
          onClose={() => setShowCreate(false)}
          onCreated={() => {
            setShowCreate(false);
            setReloadCounter((n) => n + 1);
          }}
        />
      )}
    </AppShell>
  );
}
