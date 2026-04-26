export function formatAmount(value: number | string): string {
  return Number(value).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
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
