/**
 * Shared credit-card utilization math. Single home so the accounts-page
 * subline and the CreditUtilizationWidget can't drift (F1). Liabilities are
 * stored NEGATIVE, so an owed card has a negative balance.
 */
export interface CreditUtilization {
  outstanding: number;
  utilizationPct: number;
  available: number;
  over: number;
}

export function creditUtilization(balance: number, creditLimit: number): CreditUtilization {
  const outstanding = Math.max(0, -balance);
  const utilizationPct = creditLimit > 0 ? (outstanding / creditLimit) * 100 : 0;
  const available = creditLimit + balance;
  const over = outstanding - creditLimit;
  return { outstanding, utilizationPct, available, over };
}
