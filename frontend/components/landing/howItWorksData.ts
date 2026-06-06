export type HowItWorksStep = { readonly title: string; readonly body: string };

// "How it works" steps shared by the in-page render (HowItWorks.tsx) and
// the JSON-LD HowTo block (app/page.tsx) so the structured data can't drift
// from what users see. Same pattern as faqData.ts.
//
// No em-dashes in customer copy (locked policy feedback_no_em_dashes).
export const howItWorksSteps: ReadonlyArray<HowItWorksStep> = [
  {
    title: "Connect your accounts",
    body:
      "Import a CSV from your bank or pull in transactions one by one. The Better Decision keeps the data in your org, never sold, never shared.",
  },
  {
    title: "Categorize as you go",
    body:
      "Auto-categorization learns from your edits. Spending breakdowns and budgets update in the same view, so you decide on the page you read.",
  },
  {
    title: "See what comes next",
    body:
      "Recurring bills, forecasts, and a single per-period view show what's coming. No surprises at the end of the month.",
  },
];
