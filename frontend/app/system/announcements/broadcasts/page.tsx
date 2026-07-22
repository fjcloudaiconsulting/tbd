"use client";

// Email-broadcasts tab (spec `specs/2026-07-20-broadcast-admin-ui-design.md`).
// This file covers list + compose only (Task 4); the send flow, progress
// polling, delivery breakdown, and recipients drill-down land in Task 5.

import { FormEvent, useCallback, useEffect, useState } from "react";

import AnnouncementsLayout from "@/components/AnnouncementsLayout";
import BroadcastDetail from "@/components/broadcasts/BroadcastDetail";
import ConfirmModal from "@/components/ui/ConfirmModal";
import Spinner from "@/components/ui/Spinner";
import {
  BROADCAST_ERROR_COPY,
  broadcastErrorCode,
  createBroadcast,
  deleteBroadcast,
  listBroadcasts,
} from "@/lib/broadcasts";
import { extractErrorMessage } from "@/lib/api";
import type { Broadcast, BroadcastStatus } from "@/lib/types";
import {
  badgeError,
  badgeInfo,
  badgeNeutral,
  badgeSuccess,
  btnDanger,
  btnLink,
  btnPrimary,
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
  input,
  label,
} from "@/lib/styles";

// Reuses the announcements-page badge token pattern (design tokens only,
// no raw palette - No Off-Token, checked by check-design-tokens.sh).
const STATUS_BADGE: Record<BroadcastStatus, string> = {
  draft: badgeNeutral,
  sending: badgeInfo,
  completed: badgeSuccess,
  failed: badgeError,
};

const STATUS_LABEL: Record<BroadcastStatus, string> = {
  draft: "Draft",
  sending: "Sending",
  completed: "Completed",
  failed: "Failed",
};

export default function SystemBroadcastsPage() {
  const [items, setItems] = useState<Broadcast[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const [formSubject, setFormSubject] = useState("");
  const [formBody, setFormBody] = useState("");

  const [selected, setSelected] = useState<Broadcast | null>(null);

  // Draft-only delete: the row Delete button stages a target here; the
  // ConfirmModal drives the actual call so a single click can't erase a draft.
  const [deleteTarget, setDeleteTarget] = useState<Broadcast | null>(null);
  const [deleting, setDeleting] = useState(false);

  const handleBroadcastUpdate = useCallback((updated: Broadcast) => {
    setSelected(updated);
    setItems((prev) => prev.map((it) => (it.id === updated.id ? updated : it)));
  }, []);

  async function handleDeleteConfirmed() {
    if (!deleteTarget) return;
    const target = deleteTarget;
    setError("");
    setDeleting(true);
    try {
      await deleteBroadcast(target.id);
      setItems((prev) => prev.filter((it) => it.id !== target.id));
      // Close the detail panel if it was showing the now-deleted draft.
      setSelected((cur) => (cur?.id === target.id ? null : cur));
      setDeleteTarget(null);
    } catch (err) {
      // A stale tab may hit 409 broadcast_not_draft (someone sent it since
      // the list loaded) — surface the coded copy; refresh so the row's
      // real status shows and the Delete button drops off.
      const code = broadcastErrorCode(err);
      setError(
        (code && BROADCAST_ERROR_COPY[code]) || extractErrorMessage(err),
      );
      setDeleteTarget(null);
      void loadItems();
    } finally {
      setDeleting(false);
    }
  }

  const loadItems = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listBroadcasts();
      setItems(data.items);
    } catch (err) {
      setError(extractErrorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data fetch: loadItems() populates the broadcasts list on mount
    void loadItems();
  }, [loadItems]);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setSubmitting(true);
    try {
      const draft = await createBroadcast(formSubject, formBody);
      setItems((prev) => [draft, ...prev]);
      setFormSubject("");
      setFormBody("");
    } catch (err) {
      setError(extractErrorMessage(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <AnnouncementsLayout activeTab="/system/announcements/broadcasts">
      {error && <p className={`${errorCls} mb-4`}>{error}</p>}

      <div className={`${card} mb-6`}>
        <div className={cardHeader}>
          <h2 className={cardTitle}>New broadcast</h2>
        </div>
        <form
          onSubmit={handleSubmit}
          className="p-6 grid grid-cols-1 gap-4"
          data-testid="broadcast-compose-form"
        >
          <div>
            <label htmlFor="broadcast-subject" className={label}>
              Subject
            </label>
            <input
              id="broadcast-subject"
              value={formSubject}
              onChange={(e) => setFormSubject(e.target.value)}
              className={input}
              maxLength={200}
              required
              data-testid="broadcast-form-subject"
            />
          </div>
          <div>
            <label htmlFor="broadcast-body" className={label}>
              Body
            </label>
            <textarea
              id="broadcast-body"
              value={formBody}
              onChange={(e) => setFormBody(e.target.value)}
              className={`${input} min-h-[160px]`}
              required
              data-testid="broadcast-form-body"
            />
            <p className="mt-1 text-[11px] text-text-muted">
              {"{first_name}"} inserts the recipient&apos;s name; blank lines
              become paragraphs.
            </p>
          </div>
          <p className="text-sm text-text-secondary" data-testid="broadcast-audience">
            Audience: Active + verified
          </p>
          <div className="flex justify-end">
            <button
              type="submit"
              disabled={submitting}
              className={`${btnPrimary} w-full sm:w-auto sm:min-h-0`}
              data-testid="broadcast-form-submit"
            >
              {submitting ? "Creating..." : "Create draft"}
            </button>
          </div>
        </form>
      </div>

      <div className={`${card} w-full`}>
        {loading ? (
          <Spinner />
        ) : (
          <div className="w-full overflow-x-auto">
            <table className="w-full min-w-[640px] text-sm">
              <thead>
                <tr className="border-b border-border text-left">
                  <th className="px-4 py-3 text-[11px] font-semibold uppercase tracking-wider text-text-muted">
                    Subject
                  </th>
                  <th className="px-4 py-3 text-[11px] font-semibold uppercase tracking-wider text-text-muted">
                    Status
                  </th>
                  <th className="px-4 py-3 text-[11px] font-semibold uppercase tracking-wider text-text-muted">
                    Recipients
                  </th>
                  <th className="px-4 py-3 text-[11px] font-semibold uppercase tracking-wider text-text-muted">
                    Queued
                  </th>
                  <th className="px-4 py-3 text-[11px] font-semibold uppercase tracking-wider text-text-muted">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.length === 0 && (
                  <tr>
                    <td
                      colSpan={5}
                      className="px-4 py-6 text-center text-text-muted"
                      data-testid="broadcast-empty"
                    >
                      No broadcasts yet.
                    </td>
                  </tr>
                )}
                {items.map((row) => (
                  <tr
                    key={row.id}
                    className="border-b border-border"
                    data-testid="broadcast-row-item"
                  >
                    <td className="px-4 py-3">
                      <div className="font-medium text-text-primary">
                        {row.subject}
                      </div>
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={STATUS_BADGE[row.status]}
                        data-testid={`broadcast-status-${row.id}`}
                      >
                        {STATUS_LABEL[row.status]}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-text-secondary">
                      {row.total_recipients ?? row.recipient_count ?? "-"}
                    </td>
                    <td
                      className="px-4 py-3 text-text-secondary"
                      data-testid={`broadcast-queued-${row.id}`}
                    >
                      Queued {row.sent_count}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <div className="flex items-center justify-end gap-4">
                        <button
                          onClick={() => setSelected(row)}
                          className={btnLink}
                          data-testid={`broadcast-view-${row.id}`}
                        >
                          View
                        </button>
                        {row.status === "draft" && (
                          <button
                            onClick={() => setDeleteTarget(row)}
                            className={btnDanger}
                            aria-label={`Delete "${row.subject}"`}
                            data-testid={`broadcast-delete-${row.id}`}
                          >
                            Delete
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {selected && (
        <div className="mt-6">
          <BroadcastDetail broadcast={selected} onBroadcastUpdate={handleBroadcastUpdate} />
        </div>
      )}

      <ConfirmModal
        open={deleteTarget !== null}
        variant="danger"
        title="Delete draft?"
        message={
          deleteTarget
            ? `"${deleteTarget.subject}" will be permanently deleted. This can't be undone. Only drafts can be deleted; a broadcast that has been sent is never removed.`
            : ""
        }
        confirmLabel="Delete draft"
        submitting={deleting}
        onConfirm={handleDeleteConfirmed}
        onCancel={() => setDeleteTarget(null)}
      />
    </AnnouncementsLayout>
  );
}
