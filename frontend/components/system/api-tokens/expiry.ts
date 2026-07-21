// Shared expiry / scope presentation helpers for the PAT management UI.
// Kept out of the components so both TokenList and its tests reason about the
// same thresholds (which mirror the platform reminder job: 14d amber, 3d red).

import type { ApiTokenScope } from "@/lib/types";

export const AMBER_THRESHOLD_DAYS = 14;
export const RED_THRESHOLD_DAYS = 3;

export type ExpiryTone = "normal" | "warning" | "danger";

export interface ExpiryView {
  tone: ExpiryTone;
  label: string;
}

const MS_PER_DAY = 24 * 60 * 60 * 1000;

// Whole days from `nowMs` until `expiresAt`, rounded up so "in 0 days" only
// appears once the moment has actually passed.
export function daysUntil(expiresAt: string, nowMs: number): number {
  return Math.ceil((Date.parse(expiresAt) - nowMs) / MS_PER_DAY);
}

// Amber within 14 days, red within 3, plain otherwise. The caller decides how
// to render an already-expired/revoked token (handled by the status badge).
export function expiryView(expiresAt: string, nowMs: number): ExpiryView {
  const days = daysUntil(expiresAt, nowMs);
  const label = days <= 0 ? "Expired" : `in ${days} day${days === 1 ? "" : "s"}`;
  let tone: ExpiryTone = "normal";
  if (days <= RED_THRESHOLD_DAYS) tone = "danger";
  else if (days <= AMBER_THRESHOLD_DAYS) tone = "warning";
  return { tone, label };
}

// Human label for the coarse method-scope. "Read-only" vs "Read & write" is
// the exact copy the mint radio uses, so the list stays consistent with it.
export function scopeLabel(scope: string): string {
  return scope === "write" ? "Read & write" : "Read-only";
}

export const SCOPE_OPTIONS: ReadonlyArray<{ value: ApiTokenScope; label: string; hint: string }> = [
  { value: "read", label: "Read-only", hint: "Safe GET/HEAD requests only." },
  {
    value: "write",
    label: "Read & write",
    hint: "All methods, including POST/PUT/PATCH/DELETE.",
  },
];

// The four allowed expiry presets (days). Default is 30, capped at 90.
export const EXPIRY_PRESETS = [7, 30, 60, 90] as const;
export const DEFAULT_EXPIRY_DAYS = 30;

// Short YYYY-MM-DD render for created / last-used timestamps (naive-UTC wire
// shape). Returns "Never" for a null last-used stamp.
export function shortDate(value: string | null): string {
  if (!value) return "Never";
  return value.slice(0, 10);
}
