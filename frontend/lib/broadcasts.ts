// Thin apiFetch wrappers for the superadmin email-broadcast admin UI
// (spec `specs/2026-07-20-broadcast-admin-ui-design.md`). Mirrors the
// routes in `backend/app/routers/admin_broadcasts.py`.
import { apiFetch, ApiResponseError } from "@/lib/api";
import type {
  Broadcast,
  BroadcastPreview,
  BroadcastRecipient,
  ListEnvelope,
} from "@/lib/types";

const BASE = "/api/v1/admin/broadcasts";

export async function listBroadcasts(): Promise<ListEnvelope<Broadcast>> {
  return apiFetch<ListEnvelope<Broadcast>>(BASE);
}

// `segment` is hardcoded to "active_verified" (Ruling R7.2) — v1 ships a
// single audience segment; the compose form never lets the operator pick one.
export async function createBroadcast(
  subject: string,
  body_template: string,
): Promise<Broadcast> {
  return apiFetch<Broadcast>(BASE, {
    method: "POST",
    body: JSON.stringify({ subject, body_template, segment: "active_verified" }),
  });
}

export async function getBroadcast(id: number): Promise<Broadcast> {
  return apiFetch<Broadcast>(`${BASE}/${id}`);
}

export async function previewBroadcast(id: number): Promise<BroadcastPreview> {
  return apiFetch<BroadcastPreview>(`${BASE}/${id}/preview`);
}

// "Send test to me" — dry-runs the broadcast to the calling superadmin's
// own address and stamps dry_run_sent_at (the mandatory pre-send gate).
export async function dryRunBroadcast(id: number): Promise<Broadcast> {
  return apiFetch<Broadcast>(`${BASE}/${id}/dry-run`, { method: "POST" });
}

// Typed-confirm send: the operator must echo back the exact subject and
// recipient count they were shown so a stale tab can't fire a send.
export async function sendBroadcast(
  id: number,
  confirm_subject: string,
  confirm_recipient_count: number,
): Promise<Broadcast> {
  return apiFetch<Broadcast>(`${BASE}/${id}/send`, {
    method: "POST",
    body: JSON.stringify({ confirm_subject, confirm_recipient_count }),
  });
}

export async function resumeBroadcast(id: number): Promise<Broadcast> {
  return apiFetch<Broadcast>(`${BASE}/${id}/resume`, { method: "POST" });
}

export async function listRecipients(
  id: number,
  page: number,
  pageSize: number,
): Promise<ListEnvelope<BroadcastRecipient>> {
  const offset = page * pageSize;
  return apiFetch<ListEnvelope<BroadcastRecipient>>(
    `${BASE}/${id}/recipients?limit=${pageSize}&offset=${offset}`,
  );
}

// Ruling R7.1: the backend raises coded 4xx errors as `detail={"code": ...}`
// with NO `message` field, so `apiFetch`'s `err.code` extraction (which only
// fires when `detail.message` is a string) never populates for these errors.
// Callers must read the code straight off `err.detail` instead. Do NOT
// modify shared `apiFetch` for this — it's a broadcast-specific error shape.
export function broadcastErrorCode(err: unknown): string | undefined {
  return err instanceof ApiResponseError
    && err.detail
    && typeof err.detail === "object"
    ? (err.detail as { code?: string }).code
    : undefined;
}

// Friendly copy for the six coded errors the broadcast endpoints raise.
// No em dashes (house style) — hyphens/en dashes only.
export const BROADCAST_ERROR_COPY: Record<string, string> = {
  dry_run_required:
    "Send a test to yourself first - the Send button unlocks once a dry run has gone out.",
  confirm_subject_mismatch:
    "The subject changed since this draft was loaded. Refresh and try again.",
  confirm_count_mismatch:
    "The recipient count changed since this draft was loaded. Refresh and try again.",
  recipient_cap_exceeded:
    "This broadcast targets more recipients than the per-send cap allows.",
  broadcast_not_draft:
    "This broadcast is no longer a draft, so it can't be sent or dry-run again.",
  invalid_template_token:
    "A '%' in the subject or body isn't allowed. Remove it and try again.",
};
