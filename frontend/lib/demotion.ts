// Shared copy for the one irreversible side-effect a delete can have.
//
// TBD-294. Deleting a row that another row was marked a duplicate OF marks
// that other row REJECTED. REJECTED is terminal and unreachable through the
// edit API, so the change is irreversible and the user has to be told.
//
// TBD-312. Declared ONCE and used at every call site on purpose, following
// the precedent set by `canPromoteToRecurring` in app/transactions/page.tsx:
// divergence between sites is exactly how the previous hole in this area
// survived. Two surfaces describing the SAME server-side act must not drift
// into describing it differently, and when the consequence changes exactly
// one of them would otherwise be updated.
//
// This is the consequence sentence only. Each page composes its own lead-in
// (the transactions page has a dedicated notice, the recurring page appends
// to its success message), because the ticket asks for identical copy, not
// identical chrome.
export function demotionNotice(demotedIds: number[]): string {
  if (demotedIds.length === 0) return "";
  const n = demotedIds.length;
  return n === 1
    ? "1 matched duplicate was marked rejected. It no longer counts toward balances or reports."
    : `${n} matched duplicates were marked rejected. They no longer count toward balances or reports.`;
}
