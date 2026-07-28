/**
 * User-friendly error mapping for settings forms.
 *
 * Backend MFA and settings endpoints return HTTPException(status, detail)
 * with plain text. Surfacing raw detail strings ("Invalid TOTP code",
 * "Invalid or expired MFA token") works but is inconsistent in tone and
 * occasionally leaks shape (e.g. "MFA configuration error, contact
 * support"). This helper maps `ApiResponseError` instances to short,
 * friendly, recoverable sentences without ever revealing auth-sensitive
 * state (account existence, secret state, exact reuse history).
 *
 * Pattern: status-first, with the raw detail used only when the server
 * already chose a safe, customer-facing message we want to keep. Anything
 * unrecognised falls through to the supplied fallback.
 *
 * Keep all messages free of em-dashes per user copy policy
 * (feedback_no_em_dashes).
 */

import { ApiResponseError } from "@/lib/api";

export interface ErrorMapOptions {
  fallback?: string;
}

/**
 * MFA setup / verify / enable errors.
 *
 * Endpoints covered:
 *   POST /auth/mfa/setup
 *   POST /auth/mfa/enable
 */
export function mapMfaSetupError(
  err: unknown,
  { fallback = "Something went wrong. Try again." }: ErrorMapOptions = {},
): string {
  if (!(err instanceof ApiResponseError)) {
    return err instanceof Error ? err.message : fallback;
  }
  switch (err.status) {
    case 400:
      // Server may say "Invalid TOTP code" (verify), "MFA is already
      // enabled" (race), or "Call /mfa/setup first" (state drift).
      if (/already enabled/i.test(err.message)) {
        return "Two-factor authentication is already on. Refresh the page.";
      }
      if (/totp/i.test(err.message) || /code/i.test(err.message)) {
        return "That code did not match. Codes refresh every 30 seconds, so use the latest one.";
      }
      return "We could not start setup. Try again in a moment.";
    case 401:
      return "That code did not match. Try the next code your app shows.";
    case 429:
      return "Too many attempts. Wait a minute and try again.";
    case 503:
      return "Two-factor setup is temporarily unavailable. Try again later.";
    default:
      return fallback;
  }
}

/**
 * MFA disable errors.
 *
 * Endpoint: POST /auth/mfa/disable
 * Treat 401/403 the same way (bad password) without distinguishing.
 */
export function mapMfaDisableError(
  err: unknown,
  { fallback = "We could not disable two-factor. Try again." }: ErrorMapOptions = {},
): string {
  if (!(err instanceof ApiResponseError)) {
    return err instanceof Error ? err.message : fallback;
  }
  switch (err.status) {
    case 400:
      if (/not enabled/i.test(err.message)) {
        return "Two-factor authentication is not on. Refresh the page.";
      }
      return fallback;
    case 401:
    case 403:
      return "That password did not match. Check it and try again.";
    case 429:
      return "Too many attempts. Wait a minute and try again.";
    default:
      return fallback;
  }
}

/**
 * MFA recovery-code regeneration errors.
 *
 * Endpoint: POST /auth/mfa/recovery-codes
 */
export function mapMfaRegenerateError(
  err: unknown,
  { fallback = "We could not generate new codes. Try again." }: ErrorMapOptions = {},
): string {
  if (!(err instanceof ApiResponseError)) {
    return err instanceof Error ? err.message : fallback;
  }
  switch (err.status) {
    case 400:
      if (/not enabled/i.test(err.message)) {
        return "Two-factor authentication is not on. Turn it on first.";
      }
      return fallback;
    case 401:
    case 403:
      return "That password did not match. Check it and try again.";
    case 429:
      return "Too many attempts. Wait a minute and try again.";
    default:
      return fallback;
  }
}

/**
 * Billing-cycle save errors.
 *
 * Endpoint: PUT /settings/billing-cycle
 */
export function mapBillingCycleError(
  err: unknown,
  { fallback = "We could not save the billing cycle. Try again." }: ErrorMapOptions = {},
): string {
  if (!(err instanceof ApiResponseError)) {
    return err instanceof Error ? err.message : fallback;
  }
  switch (err.status) {
    case 400:
    case 422:
      return "Pick a whole number between 1 and 28.";
    case 403:
      return "You do not have permission to change the billing cycle.";
    case 409:
      // UNREACHABLE from PUT /billing-cycle as of TBD-239, and kept on
      // purpose. That handler used to re-anchor the open period and could
      // answer `billing_period_exists` or `budget_period_conflict`; the
      // re-anchor is gone, so the endpoint now has no 409 path at all.
      // Deleting this branch would only mean re-deriving it: TBD-235
      // brings re-anchoring back as an explicit confirmed action, and it
      // raises ConflictError with exactly this shape. (TBD-241 shipped
      // `close_period`'s bound without adding a reachable 409 to THIS
      // endpoint; its own defence-in-depth 409 branch lives on
      // `mapBillingPeriodCloseError` below.) Until then it costs one
      // switch case and
      // guarantees a stray 409 never reaches a user as a bare fallback.
      //
      // The server raises ConflictError with a specific, already
      // customer-facing sentence: which category's budget, or which
      // period, already sits on the destination start date. That detail
      // is the entire point of the 409, so keep it rather than flattening
      // it into the generic fallback a 500 would show.
      return err.message.trim() || "That day collides with a period or budget that already exists. Pick a different day.";
    case 429:
      return "Too many save attempts. Wait a moment and try again.";
    default:
      return fallback;
  }
}

/**
 * Billing-period close errors.
 *
 * Endpoint: POST /settings/billing-period/close
 */
export function mapBillingPeriodCloseError(
  err: unknown,
  { fallback = "We could not close the period. Try again." }: ErrorMapOptions = {},
): string {
  if (!(err instanceof ApiResponseError)) {
    return err instanceof Error ? err.message : fallback;
  }
  switch (err.status) {
    case 400:
      if (/already.*closed/i.test(err.message) || /no.*open/i.test(err.message)) {
        return "This period is already closed. Refresh the page to see the next one.";
      }
      // TBD-241 D1. The server message is pinned as "Close date cannot be in
      // the future" (`billing_service.close_period`), so the predicate and the
      // sentence are not written against each other by guesswork. Rewording
      // either one without the other drops this back to the bare fallback.
      if (/cannot be in the future/i.test(err.message)) {
        return "That close date is in the future. Pick today or an earlier day.";
      }
      return fallback;
    case 403:
      return "You do not have permission to close the period.";
    case 409:
      // TBD-241 D7 — defence in depth, NOT a reachable path. The close's
      // budget re-anchor is the identity case, whose pre-flight carries a
      // contradictory pair of `period_start` predicates and so matches
      // nothing; its IntegrityError backstop is unreachable under the
      // service's normative operation order; and the UPDATE changes only
      // `period_end`, which is not part of `uq_budget_org_cat_period`. The
      // mapper had no `case 409` at all, so a stray one would have reached the
      // user as the generic fallback with the server's specific, already
      // customer-facing sentence thrown away.
      return err.message.trim() || "Something else changed this period while you were closing it. Refresh and try again.";
    case 429:
      return "Too many attempts. Wait a moment and try again.";
    default:
      return fallback;
  }
}

/**
 * Client-side billing-cycle-day validation. Returns null when the value
 * is acceptable, or a short reason string otherwise. Mirrors server
 * constraint (whole number, 1 to 28 inclusive).
 *
 * Days 29 to 31 are rejected because they do not exist in every month;
 * the billing period rollover needs a day that always lands.
 */
export function validateBillingCycleDay(raw: string): string | null {
  const trimmed = raw.trim();
  if (trimmed === "") return "Enter a day between 1 and 28.";
  if (!/^\d+$/.test(trimmed)) return "Use digits only.";
  const day = Number(trimmed);
  if (!Number.isInteger(day) || day < 1 || day > 28) {
    return "Pick a whole number between 1 and 28.";
  }
  return null;
}
