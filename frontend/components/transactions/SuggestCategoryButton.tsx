"use client";

import { useState } from "react";

import { apiFetch, extractErrorMessage } from "@/lib/api";

/**
 * LAI.1 — "Suggest category" affordance for the transaction edit row.
 *
 * Calls POST /api/v1/ai/categorize. On success the parent receives the
 * suggested category_id (via ``onSuggested``) and pre-fills the
 * category dropdown — but never auto-applies. The user still has to
 * click Save on the edit row to commit.
 *
 * Soft-fail posture: any non-200 response surfaces inline as a short
 * message under the button and otherwise leaves the form alone. The
 * frontend MUST NOT crash if the AI service is unconfigured (412) or
 * the org has hit its cap (402); those are operator concerns, not user
 * errors.
 *
 * Visibility: the parent decides whether to render this component at
 * all. The current convention is "show only when the org has the
 * ``ai.autocategorize`` feature AND at least one valid AI credential".
 * The backend re-checks both invariants on every request.
 */
interface Suggestion {
  category_id: number;
  category_name: string;
  confidence: number;
  reasoning: string;
}

export interface SuggestCategoryButtonProps {
  transactionId: number;
  onSuggested: (suggestion: Suggestion) => void;
  /** Optional test-id prefix so callers can scope queries. */
  testIdPrefix?: string;
  /** Optional disabled override (e.g. while the row is saving). */
  disabled?: boolean;
  className?: string;
}

export default function SuggestCategoryButton({
  transactionId,
  onSuggested,
  testIdPrefix = "ai-suggest",
  disabled = false,
  className = "",
}: SuggestCategoryButtonProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastSuggestion, setLastSuggestion] = useState<Suggestion | null>(null);

  const handleClick = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await apiFetch<Suggestion>("/api/v1/ai/categorize", {
        method: "POST",
        body: JSON.stringify({ transaction_id: transactionId }),
      });
      setLastSuggestion(result);
      onSuggested(result);
    } catch (caught) {
      setError(
        extractErrorMessage(caught, "Couldn't fetch a suggestion right now."),
      );
    } finally {
      setBusy(false);
    }
  };

  // Confidence is rendered subtly per the spec — small dim text, not a
  // headline indicator. It's advisory; the LLM doesn't always know.
  const confidenceLabel = lastSuggestion
    ? `${Math.round(lastSuggestion.confidence * 100)}% confidence`
    : null;

  return (
    <div className={`flex flex-wrap items-center gap-2 ${className}`}>
      <button
        type="button"
        onClick={handleClick}
        disabled={busy || disabled}
        className="inline-flex items-center gap-1 rounded-md border border-border bg-surface px-2 py-1 text-xs text-text-secondary transition hover:bg-surface-raised hover:text-text-primary disabled:opacity-50"
        data-testid={`${testIdPrefix}-button`}
        aria-label="Suggest a category using AI"
      >
        {busy ? (
          <>
            <span
              className="inline-block h-3 w-3 animate-spin rounded-full border border-current border-t-transparent"
              aria-hidden="true"
            />
            Thinking…
          </>
        ) : (
          <>
            <span aria-hidden="true">✨</span>
            Suggest category
          </>
        )}
      </button>
      {confidenceLabel && !error ? (
        <span
          className="text-[11px] text-text-muted"
          data-testid={`${testIdPrefix}-confidence`}
          title={lastSuggestion?.reasoning}
        >
          {lastSuggestion?.category_name} · {confidenceLabel}
        </span>
      ) : null}
      {error ? (
        <span
          className="text-[11px] text-text-muted"
          data-testid={`${testIdPrefix}-error`}
        >
          {error}
        </span>
      ) : null}
    </div>
  );
}
