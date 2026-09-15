import { currencyPrefix } from "@/lib/currencies";

// Hide balances (TBD-527): a per-device flag every money formatter reads.
// ponytail: a module store, no provider and no pre-paint script. That is safe
// only because AppShell renders a spinner while `loading || !user`, so neither
// SSR nor hydration ever contains a figure. A money page outside that gate
// would paint unmasked first; add a pre-paint script if one ever exists.
const HIDE_KEY = "tbd-hide-balances";
export const BALANCE_MASK = "•••••";
let hidden: boolean | undefined;
const listeners = new Set<() => void>();

export function isBalancesHidden(): boolean {
  if (hidden === undefined) {
    if (typeof window === "undefined") return false;
    try {
      hidden = window.localStorage.getItem(HIDE_KEY) === "1";
    } catch {
      hidden = false;
    }
  }
  return hidden;
}

export function setBalancesHidden(value: boolean): void {
  hidden = value;
  try {
    window.localStorage.setItem(HIDE_KEY, value ? "1" : "0");
  } catch {
    // Storage blocked: the choice lasts for this page load only.
  }
  listeners.forEach((fn) => fn());
}

// No cross-tab `storage` listener: another open tab picks the change up on
// reload, which is fine for a per-device preference.
export function subscribeBalancesHidden(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

// Amounts inside SERVER-BUILT text. The shapes, read off the backend:
//   - notification body: `f"{owed:,.2f}"` -> "1,240.00 EUR" (grouped)
//   - budget draft / rebalance reasoning: `{x:.2f}` -> "7373.37" (unsigned)
//   - scenario expected_outcome: `_q(delta)` = str(quantize) -> "7373.37"
//   - balance adjustment: `f"{old_balance} -> {target_balance}"`. old is a
//     Numeric(12,2) column ("-120.50"), but target is the request Decimal and
//     the modal posts a JS number, so it prints "7400" or "7400.5": the
//     second alternative masks whatever follows "-> ".
// The decimal branch refuses a digit or dot on either side, so dotted dates
// and versions in bank descriptions survive ("15.09.2026", "2026.09.15",
// "v2.10.3"), as does "12.50%". A dot is refused after the amount only when a
// digit follows it, so a sentence-final "about 7373.37." still masks.
// ⚠ A bare "14.35" IS masked: it is indistinguishable from an amount, and a
// masked clock time costs a glance while an unmasked amount is the leak.
// (Leading capture groups, not lookbehinds: tsconfig targets ES2017.)
const MONEY_IN_TEXT = /(-> )-?\d+(?:\.\d+)?|(^|[^\d.])-?\d[\d,]*\.\d{2}(?![\d%]|\.\d)/g;

/** Mask every amount in free text when balances are hidden; else unchanged. */
export function maskMoneyText(text: string): string {
  if (!isBalancesHidden()) return text;
  return text.replace(
    MONEY_IN_TEXT,
    (_m, arrow?: string, lead?: string) => `${arrow ?? lead ?? ""}${BALANCE_MASK}`,
  );
}

// ⚠ Fixed length and no sign: the mask must not leak magnitude or direction.
export function formatAmount(value: number | string): string {
  if (isBalancesHidden()) return BALANCE_MASK;
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

/**
 * A money figure with its currency: `"€1,234.56"`, `"CHF 1,234.56"` (TBD-503).
 *
 * ⚠ PLACEMENT IS AN OPERATOR RULING (2026-09-08): the symbol always LEADS,
 * while grouping and decimals keep following the viewer's locale. So a German
 * viewer sees `€ 1.234,56` rather than the `1.234,56 €` they would write. That
 * is deliberate — predictability and screenshot stability were judged to
 * matter more for a product whose copy is English throughout.
 *
 * ⚠⚠ DO NOT REWRITE THIS AS `Intl.NumberFormat(style: "currency")`. Placing
 * the symbol per locale is precisely the option that was rejected, and
 * reaching for it and then "fixing" the order is how this ends up with two
 * formatting paths that disagree — the drift TBD-503 exists to remove.
 *
 * With no currency the output is byte-identical to `formatAmount`, which is
 * what preserves the multi-currency gate: those orgs keep seeing bare numbers.
 */
export function formatMoney(
  value: number | string,
  currency?: string | null,
): string {
  const prefix = currencyPrefix(currency);
  const formatted = formatAmount(value);
  // ⚠ The SIGN goes outside the symbol: "-€13.14", not "€-13.14". Discovered
  // while converting the reconcile rows, which build a signed string from a
  // positive amount plus a type discriminator ("+€45.06"). Without this the
  // two paths would render negatives differently on the same screen.
  //
  // Handles both the ASCII hyphen-minus and U+2212, which some locales use.
  const minus = formatted.match(/^[-\u2212]/);
  if (minus && prefix) {
    return `${minus[0]}${prefix}${formatted.slice(1)}`;
  }
  return `${prefix}${formatted}`;
}
