import type { RecurringTransaction } from "@/lib/types";

type SeriesState = Pick<
  RecurringTransaction,
  "is_active" | "occurrence_count" | "occurrences_elapsed"
>;

// An instalment series whose count is spent (TBD-275). `>=`, not `===`: the
// remainder is NEGATIVE when a plan is shortened below what it already
// delivered, and that series is finished too.
export function instalmentDone(r: SeriesState): boolean {
  return r.occurrence_count != null && r.occurrences_elapsed >= r.occurrence_count;
}

// The client twin of the backend's `active_series_filter`: the series may
// still deliver an occurrence.
export function seriesRunning(r: SeriesState): boolean {
  return r.is_active && !instalmentDone(r);
}
