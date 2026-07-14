"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import AppShell from "@/components/AppShell";
import Spinner from "@/components/ui/Spinner";
import ConfirmModal from "@/components/ui/ConfirmModal";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { hasPlatformPermission } from "@/lib/auth";
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
  success as successCls,
} from "@/lib/styles";
import type {
  PermissionCatalogResponse,
  RoleDetail,
  RoleUpdatePayload,
} from "@/lib/types";

export default function AdminRoleDetailPage() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const params = useParams<{ id: string }>();
  const roleId = params?.id ? Number(params.id) : NaN;

  const [role, setRole] = useState<RoleDetail | null>(null);
  const [catalog, setCatalog] = useState<PermissionCatalogResponse | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [fetching, setFetching] = useState(true);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [showConfirmDelete, setShowConfirmDelete] = useState(false);

  useEffect(() => {
    if (loading) return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (!hasPlatformPermission(user, "roles.manage")) {
      router.replace("/dashboard");
    }
  }, [loading, user, router]);

  useEffect(() => {
    if (loading || !user || !hasPlatformPermission(user, "roles.manage")) return;
    if (!Number.isFinite(roleId)) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- route-param validation guard inside the fetch effect; setState is intentional
      setError("Invalid role id.");
      setFetching(false);
      return;
    }
    setFetching(true);
    Promise.all([
      apiFetch<RoleDetail>(`/api/v1/admin/roles/${roleId}`),
      apiFetch<PermissionCatalogResponse>("/api/v1/admin/permissions"),
    ])
      .then(([detail, perms]) => {
        setRole(detail);
        setCatalog(perms);
        setName(detail.name);
        setDescription(detail.description ?? "");
        setSelected(new Set(detail.permissions));
      })
      .catch((err) => setError(extractErrorMessage(err, "Failed to load role")))
      .finally(() => setFetching(false));
  }, [loading, user, roleId]);

  const isFrozen = role?.is_system_frozen ?? false;

  const dirty = useMemo(() => {
    if (!role) return false;
    if (name !== role.name) return true;
    if ((description ?? "") !== (role.description ?? "")) return true;
    const a = Array.from(selected).sort();
    const b = [...role.permissions].sort();
    if (a.length !== b.length) return true;
    return a.some((k, i) => k !== b[i]);
  }, [role, name, description, selected]);

  function togglePermission(key: string) {
    if (isFrozen) return;
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

  function resetForm() {
    if (!role) return;
    setName(role.name);
    setDescription(role.description ?? "");
    setSelected(new Set(role.permissions));
    setError("");
    setSuccess("");
  }

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!role || isFrozen) return;
    setError("");
    setSuccess("");
    if (!name.trim()) {
      setError("Name is required.");
      return;
    }
    setSaving(true);
    try {
      const payload: RoleUpdatePayload = {
        name: name.trim(),
        description: description.trim() ? description.trim() : null,
        permissions: Array.from(selected).sort(),
      };
      const updated = await apiFetch<RoleDetail>(
        `/api/v1/admin/roles/${role.id}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        },
      );
      setRole(updated);
      setName(updated.name);
      setDescription(updated.description ?? "");
      setSelected(new Set(updated.permissions));
      setSuccess("Role updated.");
    } catch (e) {
      setError(extractErrorMessage(e, "Failed to update role"));
    } finally {
      setSaving(false);
    }
  }

  async function destroy() {
    if (!role || isFrozen) return;
    setError("");
    setDeleting(true);
    try {
      await apiFetch<void>(`/api/v1/admin/roles/${role.id}`, {
        method: "DELETE",
      });
      router.replace("/admin/roles");
    } catch (e) {
      setError(extractErrorMessage(e, "Failed to delete role"));
      setDeleting(false);
      setShowConfirmDelete(false);
    }
  }

  if (loading || !user || !hasPlatformPermission(user, "roles.manage")) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner />
      </div>
    );
  }

  return (
    <AppShell>
      <div className="mb-2 text-xs text-text-muted">
        <Link href="/admin/roles" className="hover:text-accent">
          ← Back to roles
        </Link>
      </div>
      <div className="mb-8 flex items-center gap-3">
        <h1 className={`${pageTitle} mb-0`}>{role?.name ?? "Role"}</h1>
        {role && isFrozen && (
          <span className="rounded-full bg-accent/10 px-2 py-0.5 text-xs font-semibold uppercase tracking-wider text-accent">
            system
          </span>
        )}
      </div>

      {fetching && <Spinner />}

      {error && (
        <div className={`${errorCls} mb-4`} role="alert">
          {error}
        </div>
      )}
      {success && (
        <div className={`${successCls} mb-4`} role="status">
          {success}
        </div>
      )}

      {role && catalog && (
        <form onSubmit={save} className="space-y-6">
          <div className={card}>
            <div className={cardHeader}>
              <h2 className={cardTitle}>Details</h2>
            </div>
            <div className="space-y-4 px-6 py-4">
              <div>
                <label htmlFor="role-slug" className={labelCls}>
                  Slug
                </label>
                <input
                  id="role-slug"
                  type="text"
                  value={role.slug}
                  className={`${input} font-mono`}
                  disabled
                  readOnly
                  aria-readonly="true"
                />
                <p className="mt-1 text-xs text-text-muted">
                  Slug is immutable once created.
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
                  maxLength={120}
                  disabled={isFrozen}
                  aria-disabled={isFrozen}
                  required
                />
              </div>
              <div>
                <label htmlFor="role-description" className={labelCls}>
                  Description
                </label>
                <textarea
                  id="role-description"
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  className={`${input} min-h-[5rem]`}
                  maxLength={500}
                  disabled={isFrozen}
                  aria-disabled={isFrozen}
                />
              </div>
            </div>
          </div>

          <div className={card}>
            <div className={cardHeader}>
              <h2 className={cardTitle}>Permissions</h2>
            </div>
            <div className="px-6 py-4">
              {isFrozen && (
                <p className="mb-3 text-xs text-text-muted">
                  System role permissions are read-only.
                </p>
              )}
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
                          className={`flex items-center gap-2 rounded px-1 py-1 text-sm ${
                            isFrozen
                              ? "text-text-secondary"
                              : "text-text-primary hover:bg-surface-raised"
                          }`}
                        >
                          <input
                            type="checkbox"
                            checked={selected.has(key)}
                            onChange={() => togglePermission(key)}
                            disabled={isFrozen}
                            aria-disabled={isFrozen}
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
          </div>

          <div className="flex flex-wrap items-center justify-between gap-3">
            <button
              type="button"
              onClick={() => setShowConfirmDelete(true)}
              className="rounded-md border border-danger/40 px-4 py-2 text-sm font-medium text-danger hover:bg-danger-dim disabled:opacity-50"
              disabled={isFrozen || deleting}
              aria-disabled={isFrozen}
              title={
                isFrozen
                  ? "System roles cannot be deleted."
                  : "Delete this role"
              }
            >
              Delete role
            </button>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={resetForm}
                className={btnSecondary}
                disabled={isFrozen || !dirty || saving}
              >
                Cancel
              </button>
              <button
                type="submit"
                className={btnPrimary}
                disabled={isFrozen || !dirty || saving}
                aria-disabled={isFrozen}
                title={
                  isFrozen
                    ? "System roles cannot be edited."
                    : "Save changes"
                }
              >
                {saving ? "Saving…" : "Save changes"}
              </button>
            </div>
          </div>
        </form>
      )}

      {role && (
        <ConfirmModal
          open={showConfirmDelete}
          title="Delete role"
          message={`Delete the "${role.name}" role? This cannot be undone.`}
          confirmLabel={deleting ? "Deleting…" : "Delete"}
          cancelLabel="Cancel"
          onConfirm={destroy}
          onCancel={() => setShowConfirmDelete(false)}
          variant="danger"
        />
      )}
    </AppShell>
  );
}
