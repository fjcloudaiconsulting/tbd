/**
 * loanPayoffStatus — the single home for classifying a loan's LoanMetrics into
 * a payoff STATE and its semantic TONE. Both the accounts-page detail card
 * (LiabilityCards `LoanCard`) and the dashboard `LoanPayoffTile` consume this,
 * so neither can drift on which colour a state gets (item 7). Mirrors the shape
 * and altitude of `lib/credit.ts`: pure classification, no React, and only a
 * type-only import from lib/styles (BadgeTone) with no runtime style coupling.
 *
 * The state->tone map is the shared invariant. tone->className is a trivial
 * presentational mapping that legitimately varies per surface, so it stays out
 * of here (resolve via `badgeForTone` in lib/styles.ts, or a quieter treatment
 * on the glance tile). Labels are per-surface too (verbose on the card, terse
 * on the tile), so they are NOT defined here.
 */
import type { BadgeTone } from "@/lib/styles";
import type { LoanMetrics } from "@/lib/types";

export type LoanPayoffState = "setup" | "on_track" | "paid_off" | "interest_only";

export interface LoanPayoffStatus {
  /** null / not-yet-specified loan normalizes to "setup". */
  state: LoanPayoffState;
  tone: BadgeTone;
}

const TONE_BY_STATE: Record<LoanPayoffState, BadgeTone> = {
  setup: "info",
  on_track: "success",
  paid_off: "neutral",
  interest_only: "warning",
};

export function loanPayoffStatus(loan?: LoanMetrics | null): LoanPayoffStatus {
  const state: LoanPayoffState = loan ? loan.status : "setup";
  return { state, tone: TONE_BY_STATE[state] };
}
