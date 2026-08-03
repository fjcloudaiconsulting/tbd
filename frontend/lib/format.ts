export function formatAmount(value: number | string): string {
  return Number(value).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

// Plain "DDDD.DD" string for seeding `<input type="number">` controlled
// values. The runtime shape of `Transaction.amount` is the JSON-string
// from a Pydantic Decimal (`"19.99"`), but the TypeScript type lies and
// claims `number`; either way, going through `Number(...).toFixed(2)`
// produces a clean two-decimal string the input can render exactly.
export function toEditAmount(value: number | string): string {
  return Number(value).toFixed(2);
}

export function formatLocalDate(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

export function todayISO(): string {
  return formatLocalDate(new Date());
}

const _MONTHS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

/**
 * Format an ISO `YYYY-MM-DD` date as `Mon YYYY` (e.g. "Mar 2031"). Parses the
 * parts directly (no Date construction) so it never shifts across timezones.
 * Returns the input unchanged if it doesn't match the expected shape.
 */
export function formatMonthYear(iso: string | null | undefined): string {
  if (!iso) return "";
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  if (!m) return iso;
  const monthIdx = Number(m[2]) - 1;
  if (monthIdx < 0 || monthIdx > 11) return iso;
  return `${_MONTHS[monthIdx]} ${m[1]}`;
}

// Projected close date for an open billing period: the day before the next
// occurrence of `cycleDay`. Returns null if the inputs aren't valid.
export function projectedPeriodEnd(startISO: string, cycleDay: number): string | null {
  if (!Number.isInteger(cycleDay) || cycleDay < 1 || cycleDay > 28) return null;
  const start = new Date(startISO + "T00:00:00");
  if (Number.isNaN(start.getTime())) return null;
  const next = new Date(start.getFullYear(), start.getMonth() + 1, cycleDay);
  next.setDate(next.getDate() - 1);
  return formatLocalDate(next);
}

/**
 * Advance an ISO `YYYY-MM-DD` date by ONE recurring period (TBD-275).
 *
 * The mirror of `backend/app/services/date_utils.advance_date`, and
 * deliberately ONE STEP ONLY. It exists so promote-to-recurring can honour the
 * "`next_due_date` is the NEXT occurrence, not the one that just happened"
 * invariant without a round-trip: the FAB's date defaults to today, so sending
 * the transaction's own date made the frontier land ON the source row, which
 * generation's idempotency probe then consumed as an instalment.
 *
 * ⚠ **Do NOT grow this into a grid walker.** `occurrences_in_window` is the one
 * walk over a template's occurrence grid and it lives on the server; a second
 * copy on the client is a second thing to keep in step. This computes a single
 * seed value that the server then stores verbatim and walks from itself, so
 * there is no ongoing agreement to maintain.
 *
 * Month arithmetic clamps to the last valid day of the target month, matching
 * `dateutil.relativedelta` (Jan 31 + 1 month = Feb 28, never Mar 3, which is
 * what a naive `setMonth` overflow produces). Returns the input unchanged when
 * it does not parse.
 */
export function advanceISO(iso: string, frequency: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  if (!m) return iso;
  const y = Number(m[1]);
  const mon = Number(m[2]) - 1;
  const day = Number(m[3]);
  if (frequency === "weekly" || frequency === "biweekly") {
    const d = new Date(y, mon, day);
    d.setDate(d.getDate() + (frequency === "weekly" ? 7 : 14));
    return formatLocalDate(d);
  }
  const months =
    frequency === "quarterly" ? 3 : frequency === "yearly" ? 12 : 1;
  // Day 0 of month N+1 is the LAST day of month N, which is how the clamp is
  // read without a leap-year table.
  const lastDayOfTarget = new Date(y, mon + months + 1, 0).getDate();
  return formatLocalDate(
    new Date(y, mon + months, Math.min(day, lastDayOfTarget)),
  );
}

/** Compare two decimal-string amounts for equality without float math. */
export function equalsAmount(a: string, b: string): boolean {
  return normalizeAmount(a) === normalizeAmount(b);
}

function normalizeAmount(s: string): string {
  const sign = s.startsWith("-") ? "-" : "";
  const body = s.replace(/^-/, "");
  const [whole, frac = ""] = body.split(".");
  const wholeN = whole.replace(/^0+(?=\d)/, "") || "0";
  const fracN = frac.replace(/0+$/, "");
  return sign + (fracN ? `${wholeN}.${fracN}` : wholeN);
}
