// Shared copy and value normalisation for operator email recovery (TBD-362).
//
// An operator repointing a locked-out account's email claim writes exactly
// ONE column, `users.pending_email`. It does not verify the account, does not
// move `users.email`, and does not let the user log in. The whole point of
// the ruling is that the operator asserts an ADDRESS, never a PROOF -- the
// user still proves control by clicking the link.
//
// The sentence below is that ruling, rendered. It is declared once and used
// on BOTH the Account recovery card and the modal, following the precedent
// in `lib/demotion.ts`: two surfaces describing the SAME server-side act must
// not drift into describing it differently, and the card's copy is the one
// that matters most, because the modal is only seen after the operator has
// already committed to acting.
//
// Design: specs/2026-08-23-tbd-362-admin-email-recovery.md
export const EMAIL_RECOVERY_NOTICE =
  "This does not verify the account. A confirmation link is sent to the new " +
  "address; the account stays locked out until the user opens it.";

// The second-order consequence the DoD was silent about. Google sign-in
// matches on `users.email`, so promoting a new address also changes which
// Google identity can sign in to this account.
export const EMAIL_RECOVERY_SSO_NOTICE =
  "Changing the address also changes which Google account can sign in here: " +
  "Google sign-in matches on the account's email address.";

// Backend floor: `reason: str = Field(min_length=4, max_length=200)`.
// `reason` is REQUIRED because there is no user consent anywhere in this
// request, so the forensic note is the only contemporaneous account of why.
export const EMAIL_RECOVERY_REASON_MIN = 4;
export const EMAIL_RECOVERY_REASON_MAX = 200;

/**
 * Client-side mirror of the backend's `normalize_email`
 * (`user_service.py`: `value.strip().lower()`).
 *
 * ⚠ Used for COMPARISON and for the value that is submitted, so the
 * confirmation field accepts a legitimate case difference. A byte-equality
 * confirmation rejects `Foo@x.com` against `foo@x.com` and trains operators
 * to paste both fields, which defeats the double entry entirely.
 */
export function normalizeEmail(value: string): string {
  return value.trim().toLowerCase();
}
