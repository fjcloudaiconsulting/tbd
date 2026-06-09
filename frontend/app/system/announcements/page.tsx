"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import AppShell from "@/components/AppShell";
import ConfirmModal from "@/components/ui/ConfirmModal";
import Pagination from "@/components/ui/Pagination";
import SortableHeader from "@/components/ui/SortableHeader";
import { useTableState } from "@/lib/hooks/use-table-state";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { isSuperadmin } from "@/lib/auth";
import type { ListEnvelope } from "@/lib/types";
import {
  btnPrimary,
  btnSecondary,
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
  input,
  label,
  pageTitle,
  success as successCls,
} from "@/lib/styles";

type Severity = "info" | "promo" | "maintenance";

interface Announcement {
  id: number;
  title: string;
  body: string;
  severity: Severity;
  is_active: boolean;
  start_at: string | null;
  end_at: string | null;
  created_at: string;
  updated_at: string;
  created_by_user_id: number | null;
}

const SEVERITY_LABEL: Record<Severity, string> = {
  info: "Info",
  promo: "Promo",
  maintenance: "Maintenance",
};

// Status badge derived from the row state. Computed client-side so the
// list can render every row (active, scheduled, expired, inactive)
// without per-status filters.
type Status = "active" | "scheduled" | "expired" | "inactive";

function deriveStatus(row: Announcement, nowMs: number): Status {
  if (!row.is_active) return "inactive";
  if (row.start_at && Date.parse(row.start_at) > nowMs) return "scheduled";
  if (row.end_at && Date.parse(row.end_at) <= nowMs) return "expired";
  return "active";
}

const STATUS_BADGE: Record<Status, string> = {
  active: "bg-success-dim text-success",
  scheduled: "bg-accent/15 text-accent",
  expired: "bg-surface-raised text-text-muted",
  inactive: "bg-danger-dim text-danger",
};

// Backend-whitelisted sort keys for /api/v1/admin/announcements. Limited
// to the columns the table exposes; the derived status badge sorts on the
// is_active proxy.
const ANNOUNCEMENT_SORT_FIELDS = [
  "title",
  "severity",
  "is_active",
  "created_at",
] as const;
type AnnouncementSortField = (typeof ANNOUNCEMENT_SORT_FIELDS)[number];

// HTML datetime-local <input> expects "YYYY-MM-DDTHH:MM"; the API
// returns ISO with seconds + maybe timezone. Strip the suffix so the
// edit form can pre-fill cleanly. Empty string for null.
function toDatetimeLocal(value: string | null): string {
  if (!value) return "";
  // Backend stores naive-UTC; chop seconds + suffix.
  // "2026-05-22T14:30:00" -> "2026-05-22T14:30"
  return value.slice(0, 16);
}

function fromDatetimeLocal(value: string): string | null {
  // Empty input means "unbounded".
  if (!value) return null;
  // The control gives us "YYYY-MM-DDTHH:MM" with no zone; the backend
  // expects naive-UTC. We pass it through as-is. Add ":00" so the
  // wire shape matches an ISO datetime.
  return `${value}:00`;
}

export default function SystemAnnouncementsPage() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const [items, setItems] = useState<Announcement[]>([]);
  const [total, setTotal] = useState(0);
  const { sortField, sortDir, setSort, page, setPage, pageSize, setPageSize } =
    useTableState<AnnouncementSortField>({
      key: "system-announcements",
      defaultSortField: "created_at",
      defaultSortDir: "desc",
      allowedSortFields: ANNOUNCEMENT_SORT_FIELDS,
    });
  const [error, setError] = useState("");
  const [successMsg, setSuccessMsg] = useState("");
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Announcement | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<Announcement | null>(null);

  const [formTitle, setFormTitle] = useState("");
  const [formBody, setFormBody] = useState("");
  const [formSeverity, setFormSeverity] = useState<Severity>("info");
  const [formIsActive, setFormIsActive] = useState(true);
  const [formStartAt, setFormStartAt] = useState("");
  const [formEndAt, setFormEndAt] = useState("");

  const canManage = !!user && isSuperadmin(user);

  useEffect(() => {
    if (loading) return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (!canManage) {
      router.replace("/dashboard");
      return;
    }
  }, [loading, user, canManage, router]);

  const loadItems = useCallback(async () => {
    try {
      const params = new URLSearchParams({
        sort_by: sortField,
        sort_dir: sortDir,
        limit: String(pageSize),
        offset: String((page - 1) * pageSize),
      });
      const data = await apiFetch<ListEnvelope<Announcement>>(
        `/api/v1/admin/announcements?${params.toString()}`,
      );
      setItems(data.items);
      setTotal(data.total);
    } catch (err) {
      setError(extractErrorMessage(err));
    }
  }, [sortField, sortDir, page, pageSize]);

  useEffect(() => {
    if (canManage) void loadItems();
  }, [canManage, loadItems]);

  const handleSort = useCallback(
    (field: string) => {
      if (!(ANNOUNCEMENT_SORT_FIELDS as readonly string[]).includes(field)) return;
      const f = field as AnnouncementSortField;
      setSort(f, f === sortField && sortDir === "asc" ? "desc" : "asc");
    },
    [sortField, sortDir, setSort],
  );

  function resetForm() {
    setFormTitle("");
    setFormBody("");
    setFormSeverity("info");
    setFormIsActive(true);
    setFormStartAt("");
    setFormEndAt("");
  }

  function openCreate() {
    setEditing(null);
    setCreating(true);
    resetForm();
  }

  function openEdit(row: Announcement) {
    setEditing(row);
    setCreating(false);
    setFormTitle(row.title);
    setFormBody(row.body);
    setFormSeverity(row.severity);
    setFormIsActive(row.is_active);
    setFormStartAt(toDatetimeLocal(row.start_at));
    setFormEndAt(toDatetimeLocal(row.end_at));
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    const payload = {
      title: formTitle,
      body: formBody,
      severity: formSeverity,
      is_active: formIsActive,
      start_at: fromDatetimeLocal(formStartAt),
      end_at: fromDatetimeLocal(formEndAt),
    };
    try {
      if (editing) {
        await apiFetch(`/api/v1/admin/announcements/${editing.id}`, {
          method: "PATCH",
          body: JSON.stringify(payload),
        });
        setSuccessMsg("Announcement updated");
      } else {
        await apiFetch("/api/v1/admin/announcements", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        setSuccessMsg("Announcement created");
      }
      setTimeout(() => setSuccessMsg(""), 3000);
      setEditing(null);
      setCreating(false);
      resetForm();
      await loadItems();
    } catch (err) {
      setError(extractErrorMessage(err));
    }
  }

  async function handleDelete(row: Announcement) {
    setError("");
    try {
      await apiFetch(`/api/v1/admin/announcements/${row.id}`, {
        method: "DELETE",
      });
      setSuccessMsg("Announcement deleted");
      setTimeout(() => setSuccessMsg(""), 3000);
      await loadItems();
    } catch (err) {
      setError(extractErrorMessage(err));
    }
  }

  if (loading || !canManage) return null;

  const nowMs = Date.now();

  return (
    <AppShell>
      <div className="flex flex-col gap-2 mb-8 sm:flex-row sm:items-center sm:justify-between">
        <h1 className={pageTitle + " mb-0"}>Announcements</h1>
        <button
          onClick={openCreate}
          className={`${btnPrimary} w-full sm:w-auto sm:min-h-0`}
          data-testid="announcement-new"
        >
          + New Announcement
        </button>
      </div>

      {error && <p className={`${errorCls} mb-4`}>{error}</p>}
      {successMsg && <p className={`${successCls} mb-4`}>{successMsg}</p>}

      {(creating || editing) && (
        <div className={`${card} mb-6`} data-testid="announcement-form">
          <div className={cardHeader}>
            <h2 className={cardTitle}>
              {editing ? `Edit: ${editing.title}` : "New Announcement"}
            </h2>
          </div>
          <form onSubmit={handleSubmit} className="p-6 grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div className="sm:col-span-2">
              <label htmlFor="announcement-title" className={label}>Title</label>
              <input
                id="announcement-title"
                value={formTitle}
                onChange={(e) => setFormTitle(e.target.value)}
                className={input}
                maxLength={200}
                required
                data-testid="announcement-form-title"
              />
            </div>
            <div className="sm:col-span-2">
              <label htmlFor="announcement-body" className={label}>Body</label>
              <textarea
                id="announcement-body"
                value={formBody}
                onChange={(e) => setFormBody(e.target.value)}
                className={`${input} min-h-[120px]`}
                maxLength={5000}
                required
                data-testid="announcement-form-body"
              />
              <p className="mt-1 text-[11px] text-text-muted">
                Plain text. URLs starting with http or https render as links.
              </p>
            </div>
            <div>
              <label htmlFor="announcement-severity" className={label}>Severity</label>
              <select
                id="announcement-severity"
                value={formSeverity}
                onChange={(e) => setFormSeverity(e.target.value as Severity)}
                className={input}
                data-testid="announcement-form-severity"
              >
                <option value="info">Info</option>
                <option value="promo">Promo</option>
                <option value="maintenance">Maintenance</option>
              </select>
            </div>
            <div className="flex items-center gap-2 pt-6">
              <input
                type="checkbox"
                id="is_active"
                checked={formIsActive}
                onChange={(e) => setFormIsActive(e.target.checked)}
                data-testid="announcement-form-active"
              />
              <label htmlFor="is_active" className="text-sm text-text-secondary">
                Active
              </label>
            </div>
            <div>
              <label htmlFor="announcement-start" className={label}>Start (UTC, optional)</label>
              <input
                id="announcement-start"
                type="datetime-local"
                value={formStartAt}
                onChange={(e) => setFormStartAt(e.target.value)}
                className={input}
                data-testid="announcement-form-start"
              />
            </div>
            <div>
              <label htmlFor="announcement-end" className={label}>End (UTC, optional)</label>
              <input
                id="announcement-end"
                type="datetime-local"
                value={formEndAt}
                onChange={(e) => setFormEndAt(e.target.value)}
                className={input}
                data-testid="announcement-form-end"
              />
            </div>
            <div className="col-span-1 sm:col-span-2 flex flex-col-reverse gap-2 pt-2 sm:flex-row sm:justify-end sm:gap-3">
              <button
                type="button"
                onClick={() => {
                  setEditing(null);
                  setCreating(false);
                  resetForm();
                }}
                className={`${btnSecondary} w-full sm:w-auto min-h-[44px] sm:min-h-0`}
              >
                Cancel
              </button>
              <button
                type="submit"
                className={`${btnPrimary} w-full sm:w-auto sm:min-h-0`}
                data-testid="announcement-form-submit"
              >
                {editing ? "Save" : "Create"}
              </button>
            </div>
          </form>
        </div>
      )}

      <div className={`${card} w-full`}>
        <div className="w-full overflow-x-auto">
          <table className="w-full min-w-[640px] text-sm">
            <thead>
              <tr className="border-b border-border text-left">
                <SortableHeader
                  label="Title"
                  field="title"
                  activeField={sortField}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <SortableHeader
                  label="Severity"
                  field="severity"
                  activeField={sortField}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <SortableHeader
                  label="Status"
                  field="is_active"
                  activeField={sortField}
                  dir={sortDir}
                  onSort={handleSort}
                />
                <th className="px-4 py-3 text-[11px] font-semibold uppercase tracking-wider text-text-muted">
                  Window
                </th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody>
              {items.length === 0 && (
                <tr>
                  <td
                    colSpan={5}
                    className="px-4 py-6 text-center text-text-muted"
                    data-testid="announcement-empty"
                  >
                    No announcements yet.
                  </td>
                </tr>
              )}
              {items.map((row) => {
                const status = deriveStatus(row, nowMs);
                return (
                  <tr key={row.id} className="border-b border-border" data-testid="announcement-row-item">
                    <td className="px-4 py-3">
                      <div className="font-medium text-text-primary">{row.title}</div>
                      <div className="line-clamp-1 text-[11px] text-text-muted">
                        {row.body}
                      </div>
                    </td>
                    <td className="px-4 py-3 text-text-secondary">
                      {SEVERITY_LABEL[row.severity]}
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${STATUS_BADGE[status]}`}
                      >
                        {status}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-[11px] text-text-muted">
                      {row.start_at ? row.start_at.slice(0, 16) : "(none)"}
                      {" / "}
                      {row.end_at ? row.end_at.slice(0, 16) : "(none)"}
                    </td>
                    <td className="px-4 py-3 text-right space-x-2">
                      <button
                        onClick={() => openEdit(row)}
                        className="text-xs text-accent hover:underline"
                        data-testid="announcement-edit"
                      >
                        Edit
                      </button>
                      <button
                        onClick={() => setConfirmDelete(row)}
                        className="text-xs text-text-muted hover:text-danger"
                        data-testid="announcement-delete"
                      >
                        Delete
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {(total > pageSize || page > 1) && (
          <div className="px-4">
            <Pagination
              page={page}
              pageSize={pageSize}
              total={total}
              onPageChange={setPage}
              onPageSizeChange={setPageSize}
            />
          </div>
        )}
      </div>

      <ConfirmModal
        open={!!confirmDelete}
        title="Delete announcement"
        message={
          confirmDelete
            ? `Delete "${confirmDelete.title}"? This also clears every user dismissal for it.`
            : ""
        }
        variant="danger"
        onConfirm={() => {
          if (confirmDelete) void handleDelete(confirmDelete);
          setConfirmDelete(null);
        }}
        onCancel={() => setConfirmDelete(null)}
      />
    </AppShell>
  );
}
