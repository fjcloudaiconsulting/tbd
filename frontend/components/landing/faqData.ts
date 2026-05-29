export type FaqEntry = { readonly q: string; readonly a: string };

// FAQ entries shared by the in-page FAQ render (Faq.tsx) and the
// JSON-LD FAQPage block (app/page.tsx) so the structured data can't
// drift from what users see. Two payment-related entries and three
// tier-name parentheticals were removed in PR #378 (2026-05-29) along
// with the rest of the customer-facing payment surface; restore via
// `git revert` of #378 when the payment platform is wired.
export const faqEntries: ReadonlyArray<FaqEntry> = [
  {
    q: "Is my data secure?",
    a: "Yes. All data lives in an EU data center, encrypted at rest. Account credentials are stored as bcrypt hashes. We use HTTPS everywhere and never store your bank login credentials.",
  },
  {
    q: "Can I export my data?",
    a: "Yes. Every list view exports to CSV, and a one-click full org export is in the works. Your data is always yours.",
  },
  {
    q: "Do you use my data to train AI?",
    a: "No. Personal financial data is never used to train models. The optional AI assistant runs against a provider you choose, and you can disable it at any time.",
  },
  {
    q: "Can I delete my account?",
    a: "Yes. Account deletion is one click in Settings. It hard-deletes your data within seven days, and you receive a confirmation email when the deletion completes.",
  },
  {
    q: "Do I need to connect my bank?",
    a: "No. You can import a CSV from your bank, or add transactions manually. Direct bank connections are on the roadmap but not required to get the full value out of the app.",
  },
  {
    q: "Is it built for one person or a couple?",
    a: "Both. The data model is org-scoped, so you start as a one-person org and can invite a partner or housemate later. Each org has its own categories, accounts, and reports.",
  },
];
