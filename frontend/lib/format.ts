export function formatAmount(value: number | string): string {
  return Number(value).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export function todayISO(): string {
  return new Date().toISOString().slice(0, 10);
}
