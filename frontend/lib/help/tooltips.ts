/**
 * Tooltip content map (L5.3 — help residuals).
 *
 * One row per frequently confused field, looked up by string key. The
 * ``<HelpTooltip>`` wrapper consumes this map so future additions are
 * a one-line edit instead of an inline-JSX shuffle. Pair this with the
 * existing ``<Tooltip>`` primitive (``frontend/components/Tooltip.tsx``)
 * which owns the trigger, portal, accessibility, and reduced-motion
 * behavior.
 *
 * House style: one short sentence, no em-dashes (use commas/periods
 * or parentheses instead), plain language. Each entry can optionally
 * point at a /docs anchor via ``learnMoreSection`` so power users get
 * the full manual in a new tab.
 *
 * Keys are dot-namespaced "<feature>.<field>" so a future tooltip
 * audit can grep for every site that uses ``HelpTooltip k="tx.X"``
 * (the prop is `k`, not `key` — `key` is React-reserved).
 */

export interface HelpTooltipEntry {
  /** One short sentence. No em-dashes. */
  content: string;
  /** Optional /docs anchor for "Learn more" deep link. */
  learnMoreSection?: string;
  /** Optional ARIA label override for the trigger button. */
  triggerLabel?: string;
}

export const HELP_TOOLTIPS = {
  // Transactions
  "tx.account": {
    content:
      "Which account this transaction belongs to. Pick the account the money actually moved through, not the one it pays for.",
    learnMoreSection: "transactions",
    triggerLabel: "What does Account mean here?",
  },
  "tx.type": {
    content:
      "Expense means money going out, Income means money coming in. The Better Decision stores all amounts as positives and uses Type to set the sign.",
    learnMoreSection: "transactions",
    triggerLabel: "What is the difference between Expense and Income?",
  },
  "tx.amount": {
    content:
      "Always a positive number. The Type field above controls whether it counts as money in or money out.",
    learnMoreSection: "transactions",
    triggerLabel: "Why is Amount always positive?",
  },
  "tx.frequency": {
    content:
      "How often this transaction repeats. Weekly fires every 7 days, Monthly fires on the same day each month, Yearly fires on the same day each year.",
    learnMoreSection: "transactions",
    triggerLabel: "What does Frequency mean?",
  },
  "tx.auto-settle": {
    content:
      "When on, recurring instances flip from Pending to Settled on their scheduled date with no extra clicks. Leave off if you want to confirm each one yourself.",
    learnMoreSection: "transactions",
    triggerLabel: "What does Auto settle do?",
  },
  "tx.tags": {
    content:
      "Free form labels you can attach to a transaction. Tags do not replace categories, they live alongside them so you can group across categories (work, vacation, etc).",
    learnMoreSection: "transactions",
    triggerLabel: "What are Tags?",
  },

  // Transfers
  "transfer.category": {
    content:
      "Transfers use one shared category for both legs. Pick Transfer, Credit Card Payment, or another both type category. Leave empty to use the default Transfer category.",
    learnMoreSection: "transactions",
    triggerLabel: "Which category should a transfer use?",
  },

  // Categories
  "cat.type": {
    content:
      "Income, Expense, or Both. Both is for transfer style categories like Credit Card Payment that can carry either direction.",
    learnMoreSection: "categories",
    triggerLabel: "What does category Type mean?",
  },
  "cat.subcategory": {
    content:
      "Subcategories nest under a master. A master like Food can have child rows Groceries, Dining, and Coffee, so you can drill in without losing the parent total.",
    learnMoreSection: "categories",
    triggerLabel: "Explain category nesting",
  },

  // Budgets
  "budget.monthly-limit": {
    content:
      "The cap for this category in one billing period. Budgets reset every period and never roll over.",
    learnMoreSection: "budgets",
    triggerLabel: "What does Monthly limit mean?",
  },

  // Accounts
  "account.opening-balance": {
    content:
      "What the account held on the day you start tracking it. The Better Decision uses this as a starting point and adds your transactions on top.",
    learnMoreSection: "accounts",
    triggerLabel: "What is Opening balance?",
  },
  "account.adjust-balance": {
    content:
      "Use this when your real bank balance differs from the one The Better Decision is showing. We record a single Adjustment transaction so the difference is auditable.",
    learnMoreSection: "accounts",
    triggerLabel: "Why is there an Adjustment transaction?",
  },

  // Plans
  "plans.sandbox": {
    content:
      "Plans is a sandbox. Whatever you do here, including big life events and salary changes, never modifies your real transactions or budgets.",
    learnMoreSection: "plans",
    triggerLabel: "Does Plans change my real data?",
  },

  // Reports
  "reports.kpi": {
    content:
      "Drop in KPIs (Net Cashflow, Total Spent, Savings Rate) and charts over any date range. Layouts are saved per user.",
    learnMoreSection: "reports",
    triggerLabel: "What can I put in a report?",
  },

  // Dashboard
  "dashboard.on-track": {
    content:
      "On Track compares your budgeted spend to your actual spend. Green is within plan, yellow is close to the cap, red is over.",
    learnMoreSection: "dashboard",
    triggerLabel: "What do the On Track colors mean?",
  },
} as const satisfies Record<string, HelpTooltipEntry>;

export type HelpTooltipKey = keyof typeof HELP_TOOLTIPS;

/**
 * Returns the entry for a key. Throws in dev when the key is missing
 * so typos surface during local work; returns a soft fallback in
 * production so a single bad key never breaks a page.
 */
export function getHelpTooltip(key: HelpTooltipKey): HelpTooltipEntry {
  const entry = HELP_TOOLTIPS[key];
  if (entry) return entry;
  if (process.env.NODE_ENV !== "production") {
    throw new Error(`[HelpTooltip] unknown key: ${String(key)}`);
  }
  return { content: "" };
}
