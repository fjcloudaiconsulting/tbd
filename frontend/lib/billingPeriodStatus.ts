/**
 * TBD-242 — the ONE frontend definition of "the current billing period".
 *
 * Before this module the frontend carried three incompatible rules across ~43
 * decision sites in 10 files: `end_date === null` (most sites), calendar
 * containment (`ForecastPlansClient`), and the backend `status` field (the
 * roster page only). On a lapsed roster they disagreed, so the same row was
 * *Past* on the dashboard and *Current* on Forecasts.
 *
 * ⚠ **The fix is TWO functions, not one, and keeping them apart is the whole
 * point.** Conflating classification with selection is what produced the
 * divergence:
 *
 * | | mirrors | needs a clock? | answers |
 * |---|---|---|---|
 * | {@link periodStatus} | `billing_service.period_status` | **yes** | "what IS this row?" |
 * | {@link selectCurrentPeriod} | `billing_service.get_current_period` | **no** | "which row do I SHOW?" |
 *
 * The backend classifier's docstring is explicit that it "classifies rows, it
 * does not SELECT one", and hands selection to this ticket. So the two rules
 * genuinely are different rules — they are not two spellings of one idea, and
 * a future refactor that merges them reintroduces the bug.
 *
 * **Why selection follows the backend rather than the calendar.**
 * `resolve_period` falls back to `get_current_period` for any write that omits
 * an explicit `period_start`. If a screen displayed the calendar-containing
 * stub while the backend wrote to the newest open row, the user's edit would
 * land on a period they were not looking at. Display must agree with the
 * write target, so selection mirrors the backend exactly: newest open row.
 * That rule is clock-free, which is also what keeps SELECTION immune to the
 * timezone skew discussed below.
 */

/**
 * §2.3's five-branch partition. Mirrors
 * `billing_service.PeriodStatus` (backend/app/services/billing_service.py:1205).
 */
export type PeriodStatus =
  | "invalid"
  | "open"
  | "upcoming"
  | "current_by_calendar"
  | "past";

/** The three columns the rule reads. Any period-shaped row satisfies it. */
export interface PeriodRowLike {
  start_date: string;
  end_date: string | null;
}

/**
 * The canonical status of one row. Mirrors `billing_service.period_status`
 * (backend/app/services/billing_service.py:1414) branch for branch.
 *
 * ⚠ **The branch order is NORMATIVE, not stylistic.** The backend spec's
 * revision 1 gave four unordered predicates that were not disjoint; two
 * implementers writing the `if/else` in different orders both satisfied it
 * and produced different answers. Keep these five in this order:
 *
 * (Precisely: branches 1 and 2 are mutually exclusive — one requires
 * `end_date !== null`, the other `end_date === null` — so *their* relative
 * order is unobservable. What is observable, and what the tests pin, is that
 * branch 1 precedes branches 3-5: an inverted row with `start_date > today`
 * is `invalid`, not `upcoming`. Mutation-verified both ways.)
 *
 * 1. `invalid`             `end_date !== null && end_date < start_date`
 * 2. `open`                `end_date === null`
 * 3. `upcoming`            `start_date > today`
 * 4. `current_by_calendar` `start_date <= today <= end_date`
 * 5. `past`                `end_date < today`
 *
 * Dates are ISO `YYYY-MM-DD` strings, which compare correctly with `<`/`>`
 * lexicographically — the same property the backend's `date` comparison has.
 *
 * ⚠ **`today` must be LOCAL, via `todayISO()`** — not
 * `new Date().toISOString().slice(0, 10)`, which is UTC. Those differ for
 * roughly half the clock, and mixing them is why Forecasts and Budgets could
 * disagree about the same row for a user in UTC+13.
 *
 * Branch 1 is unreachable through shipped writers (every `end_date` writer is
 * provably non-inverting) and stays anyway, defensively: the table carries no
 * CHECK constraint, so a direct DB edit or a future writer that skips the
 * schema layer can produce one, and without branch 1 such a row matches both
 * `upcoming` and `past`.
 */
export function periodStatus(row: PeriodRowLike, today: string): PeriodStatus {
  if (row.end_date !== null && row.end_date < row.start_date) return "invalid";
  if (row.end_date === null) return "open";
  if (row.start_date > today) return "upcoming";
  if (row.start_date <= today && today <= row.end_date) return "current_by_calendar";
  return "past";
}

/** Whether a row is the open-ended one. The structural half of `periodStatus`. */
export function isOpenPeriod(row: PeriodRowLike): boolean {
  return row.end_date === null;
}

/**
 * The row a screen should treat as current. Mirrors
 * `billing_service.get_current_period` (…:76-96): the OPEN row with the
 * greatest `start_date`.
 *
 * ⚠ **Clock-free on purpose.** A calendar-containment rule here would desync
 * the display from the write target (see the module docstring). The
 * `never consults a clock` test guards this.
 *
 * ⚠ **Returns `null` rather than falling back to `periods[0]`**, so that "no
 * period is current" is expressible at all.
 *
 * ⚠ **But no caller acts on that distinction yet, and this docstring used to
 * claim otherwise.** Exactly ONE site ever wrote `?? periods[0]`
 * (`app/forecast-plans/page.tsx`), and TBD-242 deliberately KEPT that
 * fallback — the RSC still has to seed the client with something. Every other
 * caller guards with `idx >= 0` / `!== null` and then falls back to index 0
 * anyway. So on a `no_open` roster the screens still present the first row as
 * if it were current; this signature merely makes fixing that possible.
 * Surfacing it is TBD-235's job. (An earlier revision of this comment said
 * "roughly seven sites" and implied the benefit was already delivered. It was
 * not. Corrected after review — the same unreliable-description defect this
 * programme keeps hitting, reproduced here.)
 *
 * ⚠ **Ties break on the greater `id`.** `get_current_period` orders by
 * `start_date DESC` alone, because `uq_billing_period_org_start` makes ties
 * impossible *in the database*. But this function's input is an untrusted JSON
 * array, not a query result, and without a tie-break its answer would depend
 * on array order — the exact class of order-dependence this module exists to
 * remove. The tie-break is unreachable through a well-formed payload.
 */
export function selectCurrentPeriod<T extends PeriodRowLike>(
  periods: readonly T[],
): T | null {
  return periods.reduce<T | null>((best, row) => {
    if (!isOpenPeriod(row)) return best;
    if (best === null) return row;
    if (row.start_date !== best.start_date) {
      return row.start_date > best.start_date ? row : best;
    }
    // Same start_date: deterministic on id, so array order cannot decide.
    const rowId = (row as { id?: number }).id;
    const bestId = (best as { id?: number }).id;
    if (typeof rowId === "number" && typeof bestId === "number") {
      return rowId > bestId ? row : best;
    }
    return best;
  }, null);
}

/**
 * Index of {@link selectCurrentPeriod}'s answer, or `-1`.
 *
 * Exists because most call sites drive a `periodIdx` cursor and would
 * otherwise re-derive the rule with a second `findIndex`.
 */
export function selectCurrentPeriodIndex(
  periods: readonly PeriodRowLike[],
): number {
  const current = selectCurrentPeriod(periods);
  return current === null ? -1 : periods.indexOf(current);
}
