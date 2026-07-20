"use client";

// Send flow + progress + delivery + recipients drill-down for a single
// broadcast (spec `specs/2026-07-20-broadcast-admin-ui-design.md`, Task 5).
// Draft-only controls per Ruling R4; preview is text-only per Ruling R5;
// labeling honesty per Ruling R3; polling per Ruling R7.3.

import { useEffect, useRef, useState } from "react";

import SendConfirmModal from "@/components/broadcasts/SendConfirmModal";
import {
  BROADCAST_ERROR_COPY,
  broadcastErrorCode,
  dryRunBroadcast,
  getBroadcast,
  listRecipients,
  previewBroadcast,
  resumeBroadcast,
  sendBroadcast,
} from "@/lib/broadcasts";
import { extractErrorMessage } from "@/lib/api";
import type { Broadcast, BroadcastRecipient } from "@/lib/types";
import { btnLink, btnSecondary, card, cardHeader, cardTitle, error as errorCls } from "@/lib/styles";

const RECIPIENTS_PAGE_SIZE = 25;
const POLL_INTERVAL_MS = 5000;

interface Props {
  broadcast: Broadcast;
  onBroadcastUpdate: (updated: Broadcast) => void;
}

function friendlyError(err: unknown): string {
  const code = broadcastErrorCode(err);
  if (code && BROADCAST_ERROR_COPY[code]) return BROADCAST_ERROR_COPY[code];
  return extractErrorMessage(err);
}

export default function BroadcastDetail({ broadcast, onBroadcastUpdate }: Props) {
  const isDraft = broadcast.status === "draft";
  const dryRunDone = Boolean(broadcast.dry_run_sent_at);
  const recipientCount = broadcast.recipient_count ?? broadcast.total_recipients ?? 0;

  // ── Preview (R5: text in a <pre>, never HTML/iframe) ──────────────────
  const [previewText, setPreviewText] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);

  async function handlePreview() {
    setPreviewError(null);
    setPreviewLoading(true);
    try {
      const result = await previewBroadcast(broadcast.id);
      setPreviewText(result.text);
    } catch (err) {
      setPreviewError(friendlyError(err));
    } finally {
      setPreviewLoading(false);
    }
  }

  // ── Dry-run ("Send test to me") ────────────────────────────────────────
  const [dryRunMessage, setDryRunMessage] = useState<string | null>(null);
  const [dryRunError, setDryRunError] = useState<string | null>(null);
  const [dryRunLoading, setDryRunLoading] = useState(false);

  async function handleDryRun() {
    setDryRunError(null);
    setDryRunMessage(null);
    setDryRunLoading(true);
    try {
      const updated = await dryRunBroadcast(broadcast.id);
      onBroadcastUpdate(updated);
      setDryRunMessage("Test sent to your inbox.");
    } catch (err) {
      setDryRunError(friendlyError(err));
    } finally {
      setDryRunLoading(false);
    }
  }

  // ── Send (type-the-count confirm modal, double-submit guard) ──────────
  const [showSendModal, setShowSendModal] = useState(false);
  const [sendInFlight, setSendInFlight] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);

  async function handleConfirmSend() {
    if (sendInFlight) return;
    setSendInFlight(true);
    setSendError(null);
    try {
      const updated = await sendBroadcast(broadcast.id, broadcast.subject, recipientCount);
      onBroadcastUpdate(updated);
      setShowSendModal(false);
    } catch (err) {
      setSendError(friendlyError(err));
      // R4: on confirm_count_mismatch, re-fetch the broadcast to refresh the
      // shown count so the operator sees the real number before retrying.
      if (broadcastErrorCode(err) === "confirm_count_mismatch") {
        try {
          const refreshed = await getBroadcast(broadcast.id);
          onBroadcastUpdate(refreshed);
        } catch {
          // Refetch failure is non-fatal; the mismatch copy is already shown.
        }
      }
    } finally {
      setSendInFlight(false);
    }
  }

  // ── Progress polling while sending (R7.3) ──────────────────────────────
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (broadcast.status === "sending") {
      pollRef.current = setInterval(() => {
        getBroadcast(broadcast.id)
          .then(onBroadcastUpdate)
          .catch(() => {
            // Transient poll failure; try again next tick.
          });
      }, POLL_INTERVAL_MS);
    }
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [broadcast.status, broadcast.id, onBroadcastUpdate]);

  // ── Resume (sending = stalled, failed = recoverable) ───────────────────
  const [resumeLoading, setResumeLoading] = useState(false);
  const [resumeError, setResumeError] = useState<string | null>(null);
  const canResume = broadcast.status === "sending" || broadcast.status === "failed";

  async function handleResume() {
    setResumeError(null);
    setResumeLoading(true);
    try {
      const updated = await resumeBroadcast(broadcast.id);
      onBroadcastUpdate(updated);
    } catch (err) {
      setResumeError(friendlyError(err));
    } finally {
      setResumeLoading(false);
    }
  }

  // ── Recipients drill-down ───────────────────────────────────────────────
  const [recipientsOpen, setRecipientsOpen] = useState(false);
  const [recipients, setRecipients] = useState<BroadcastRecipient[]>([]);
  const [recipientsPage, setRecipientsPage] = useState(0);
  const [recipientsTotal, setRecipientsTotal] = useState(0);
  const [recipientsLoading, setRecipientsLoading] = useState(false);
  const [recipientsError, setRecipientsError] = useState<string | null>(null);

  async function loadRecipients(page: number) {
    setRecipientsLoading(true);
    setRecipientsError(null);
    try {
      const data = await listRecipients(broadcast.id, page, RECIPIENTS_PAGE_SIZE);
      setRecipients(data.items);
      setRecipientsPage(page);
      setRecipientsTotal(data.total);
    } catch (err) {
      setRecipientsError(friendlyError(err));
    } finally {
      setRecipientsLoading(false);
    }
  }

  async function handleViewRecipients() {
    setRecipientsOpen(true);
    await loadRecipients(0);
  }

  return (
    <div className={`${card} w-full`} data-testid="broadcast-detail">
      <div className={cardHeader}>
        <h2 className={cardTitle}>{broadcast.subject}</h2>
      </div>

      <div className="p-6 flex flex-col gap-4">
        {isDraft && (
          <div className="flex flex-col gap-3">
            <div className="flex flex-wrap gap-2">
              <button
                onClick={handlePreview}
                disabled={previewLoading}
                className={`${btnSecondary} min-h-0`}
                data-testid="broadcast-preview-button"
              >
                {previewLoading ? "Loading preview..." : "Preview"}
              </button>
              <button
                onClick={handleDryRun}
                disabled={dryRunLoading}
                className={`${btnSecondary} min-h-0`}
                data-testid="broadcast-dry-run-button"
              >
                {dryRunLoading ? "Sending test..." : "Send test to me"}
              </button>
              <button
                onClick={() => setShowSendModal(true)}
                disabled={!dryRunDone}
                className={`${btnSecondary} min-h-0`}
                data-testid="broadcast-send-button"
              >
                Send
              </button>
            </div>
            <p className="text-xs text-text-muted">
              Send test to me shows the real rendered email.
            </p>

            {previewError && <p className={errorCls}>{previewError}</p>}
            {previewText !== null && (
              <pre
                className="whitespace-pre-wrap rounded-md border border-border bg-surface-raised p-4 text-sm text-text-primary"
                data-testid="broadcast-preview-text"
              >
                {previewText}
              </pre>
            )}

            {dryRunError && <p className={errorCls}>{dryRunError}</p>}
            {dryRunMessage && (
              <p className="text-sm text-success" data-testid="broadcast-dry-run-message">
                {dryRunMessage}
              </p>
            )}
          </div>
        )}

        {!isDraft && (
          <>
            <div className="text-sm text-text-secondary" data-testid="broadcast-progress">
              Queued {broadcast.sent_count} / Failed {broadcast.failed_count} / Skipped{" "}
              {broadcast.skipped_count} of {broadcast.total_recipients ?? recipientCount}
            </div>

            <div className="text-sm text-text-secondary" data-testid="broadcast-delivery">
              <p>
                Delivered {broadcast.delivered_count} / Bounced {broadcast.bounced_count} (
                {broadcast.soft_bounced_count} soft) / Complaints {broadcast.complained_count}
              </p>
              <p className="mt-1 text-xs text-text-muted">
                Delivery status populates as Mailgun reports back (requires the delivery
                webhook).
              </p>
            </div>

            {canResume && (
              <div>
                <button
                  onClick={handleResume}
                  disabled={resumeLoading}
                  className={`${btnSecondary} min-h-0`}
                  data-testid="broadcast-resume-button"
                >
                  {resumeLoading ? "Resuming..." : "Resume"}
                </button>
                {resumeError && <p className={`${errorCls} mt-2`}>{resumeError}</p>}
              </div>
            )}
          </>
        )}

        <div>
          <button
            onClick={handleViewRecipients}
            className={btnLink}
            data-testid="broadcast-view-recipients-button"
          >
            View recipients
          </button>

          {recipientsOpen && (
            <div className="mt-3 w-full overflow-x-auto" data-testid="broadcast-recipients-table">
              {recipientsError && <p className={errorCls}>{recipientsError}</p>}
              {recipientsLoading ? (
                <p className="text-sm text-text-muted">Loading recipients...</p>
              ) : (
                <table className="w-full min-w-[480px] text-sm">
                  <thead>
                    <tr className="border-b border-border text-left">
                      <th className="px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-text-muted">
                        Email
                      </th>
                      <th className="px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-text-muted">
                        Status
                      </th>
                      <th className="px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-text-muted">
                        Delivery
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {recipients.map((r) => (
                      <tr key={r.id} className="border-b border-border" data-testid="broadcast-recipient-row">
                        <td className="px-3 py-2 text-text-primary">{r.email}</td>
                        <td className="px-3 py-2 text-text-secondary">{r.status}</td>
                        <td className="px-3 py-2 text-text-secondary">
                          {r.delivery_status ?? "-"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              {recipientsTotal > RECIPIENTS_PAGE_SIZE && (
                <div className="mt-2 flex justify-end gap-2">
                  <button
                    onClick={() => void loadRecipients(recipientsPage - 1)}
                    disabled={recipientsPage === 0}
                    className={`${btnSecondary} min-h-0`}
                  >
                    Previous
                  </button>
                  <button
                    onClick={() => void loadRecipients(recipientsPage + 1)}
                    disabled={(recipientsPage + 1) * RECIPIENTS_PAGE_SIZE >= recipientsTotal}
                    className={`${btnSecondary} min-h-0`}
                  >
                    Next
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      <SendConfirmModal
        open={showSendModal}
        subject={broadcast.subject}
        recipientCount={recipientCount}
        dryRunDone={dryRunDone}
        sending={sendInFlight}
        errorMessage={sendError}
        onConfirm={() => void handleConfirmSend()}
        onCancel={() => setShowSendModal(false)}
      />
    </div>
  );
}
