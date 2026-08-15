// Canonical data-policy facts quoted by customer-facing copy.
//
// The landing FAQ and the privacy policy both state the account-deletion
// window. Before TBD-343 they disagreed: the FAQ claimed seven days, the
// policy thirty. Quoting one constant from both surfaces makes that
// particular contradiction unrepresentable rather than merely tested for.
//
// These describe what the product actually does today. If a window
// changes, change it here and both surfaces follow; if a mechanism
// changes (for example, self-serve deletion ships), the copy that quotes
// these constants has to be revisited, not just the numbers.

/** Days within which an account and its data are deleted after a request. */
export const DATA_DELETION_WINDOW_DAYS = 30;

/** Days within which backups holding deleted data are rotated out. */
export const BACKUP_ROTATION_WINDOW_DAYS = 90;

/** The address that erasure and portability requests are made to. */
export const PRIVACY_CONTACT_EMAIL = "privacy@thebetterdecision.com";
