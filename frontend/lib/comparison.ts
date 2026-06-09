// frontend/lib/comparison.ts
// Single source of truth for every comparison fact shown on /compare and the
// /vs/* pages. The matrix and each /vs table render slices of THIS object so
// the facts cannot drift. No em-dashes (locked policy). Cells state a short
// factual phrase (AEO-extractable), never a bare checkmark.
//
// HONESTY: competitor cells must be verified against the live competitor sites
// before publish, especially `privacyFirstAi` (Monarch ships its own AI
// assistant) and `price` (kept generic, no dollar amounts, no permanent claim).

export type Competitor =
  | "tbd"
  | "ynab"
  | "pocketsmith"
  | "monarch"
  | "spreadsheets";

export type Dimension =
  | "forecasting"
  | "budgeting"
  | "euResidency"
  | "householdSharing"
  | "bankSync"
  | "privacyFirstAi"
  | "price";

export interface Cell {
  supported: "yes" | "no" | "partial";
  value: string;
}

// Render order (tbd always first).
export const competitorOrder: ReadonlyArray<Competitor> = [
  "tbd",
  "spreadsheets",
  "ynab",
  "pocketsmith",
  "monarch",
];

export const dimensionOrder: ReadonlyArray<Dimension> = [
  "forecasting",
  "budgeting",
  "bankSync",
  "householdSharing",
  "euResidency",
  "privacyFirstAi",
  "price",
];

export const dimensionLabels: Record<Dimension, string> = {
  forecasting: "Cash-flow forecasting",
  budgeting: "Budgeting",
  bankSync: "Live bank sync",
  householdSharing: "Household sharing",
  euResidency: "EU data residency",
  privacyFirstAi: "Bring-your-own / local AI",
  price: "Price",
};

export const competitorMeta: Record<
  Competitor,
  { name: string; whereTheyWin: string[] }
> = {
  tbd: { name: "The Better Decision", whereTheyWin: [] },
  spreadsheets: {
    name: "Spreadsheets",
    whereTheyWin: [
      "Total flexibility: anything you can express in a formula.",
      "Free forever, no account, no new tool to learn.",
      "You own the file outright and can take it anywhere.",
    ],
  },
  ynab: {
    name: "YNAB",
    whereTheyWin: [
      "Live bank sync that imports transactions automatically.",
      "A proven zero-based method with a long-running community.",
      "Deep educational content and a mature mobile app.",
    ],
  },
  pocketsmith: {
    name: "PocketSmith",
    whereTheyWin: [
      "Deeper forecasting: long-range projections and multiple scenarios.",
      "Wider bank-feed coverage and multi-currency support.",
      "A mature product with years of refinement.",
    ],
  },
  monarch: {
    name: "Monarch",
    whereTheyWin: [
      "Comprehensive live bank and investment sync for net-worth tracking.",
      "A very polished, well-reviewed interface.",
      "An established user base and ecosystem.",
    ],
  },
};

export const comparisonMatrix: Record<Dimension, Record<Competitor, Cell>> = {
  forecasting: {
    tbd: { supported: "yes", value: "Projected balance plus what-if scenarios" },
    spreadsheets: { supported: "partial", value: "Only what you build by hand" },
    ynab: { supported: "no", value: "Envelope budgeting, no forward projection" },
    pocketsmith: { supported: "yes", value: "Long-range cash-flow projections" },
    monarch: { supported: "partial", value: "Basic cash-flow projection" },
  },
  budgeting: {
    tbd: { supported: "yes", value: "Category budgets and forecast plans" },
    spreadsheets: { supported: "partial", value: "Whatever you build yourself" },
    ynab: { supported: "yes", value: "Zero-based envelope method" },
    pocketsmith: { supported: "partial", value: "Budgeting is the secondary feature" },
    monarch: { supported: "yes", value: "Category budgets" },
  },
  bankSync: {
    tbd: { supported: "no", value: "CSV and OFX import, no account linking" },
    spreadsheets: { supported: "no", value: "Manual entry" },
    ynab: { supported: "yes", value: "Live bank sync" },
    pocketsmith: { supported: "yes", value: "Live bank feeds" },
    monarch: { supported: "yes", value: "Live bank and investment sync" },
  },
  householdSharing: {
    tbd: { supported: "yes", value: "Shared organization with roles" },
    spreadsheets: { supported: "partial", value: "Share the file and hope" },
    ynab: { supported: "yes", value: "Shared accounts" },
    pocketsmith: { supported: "yes", value: "Household accounts on paid tiers" },
    monarch: { supported: "yes", value: "Shared households" },
  },
  euResidency: {
    tbd: { supported: "yes", value: "EU-hosted, processed under EU law" },
    spreadsheets: { supported: "partial", value: "Wherever you keep the file" },
    ynab: { supported: "no", value: "US-based" },
    pocketsmith: { supported: "partial", value: "New Zealand, with EU adequacy" },
    monarch: { supported: "no", value: "US-based" },
  },
  privacyFirstAi: {
    tbd: { supported: "yes", value: "Your own key or local Ollama, with spend caps" },
    spreadsheets: { supported: "no", value: "None built in" },
    ynab: { supported: "no", value: "No AI features" },
    pocketsmith: { supported: "no", value: "Opt-in AI beta, no bring-your-own or local" },
    monarch: { supported: "partial", value: "Built-in assistant, no bring-your-own or local" },
  },
  price: {
    tbd: { supported: "yes", value: "Free while in beta" },
    spreadsheets: { supported: "yes", value: "Free" },
    ynab: { supported: "no", value: "Paid subscription" },
    pocketsmith: { supported: "partial", value: "Free tier plus paid plans" },
    monarch: { supported: "no", value: "Paid subscription" },
  },
};
