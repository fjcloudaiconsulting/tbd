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
