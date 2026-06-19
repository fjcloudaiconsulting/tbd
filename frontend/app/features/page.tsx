// frontend/app/features/page.tsx
import Link from "next/link";
import type { Metadata } from "next";
import MarketingShell from "@/components/landing/MarketingShell";
import { readNonce } from "@/lib/nonce";
import { apexCanonical, apexUrl, pageSocialMeta, siteName } from "@/lib/site";

const description =
  "Everything The Better Decision does: cash-flow forecasting, budgets, recurring bills, imports, reports, household sharing, and bring-your-own or local AI, all EU-hosted.";

export const metadata: Metadata = {
  title: "Features",
  description,
  alternates: { canonical: apexCanonical("/features") },
  ...pageSocialMeta({
    title: `Features · ${siteName}`,
    description,
    path: apexCanonical("/features"),
  }),
  robots: { index: true, follow: true },
};

const orgId = `${apexUrl}/#organization`;

// featureList holds ONLY shipped features. Roadmap items are excluded by design
// (see the roadmap block below and the no-roadmap-in-featurelist test).
const shippedFeatures = [
  "Cash-flow forecasting with what-if scenarios",
  "Projected end-of-month balance per account",
  "Category budgets and forecast plans",
  "Recurring income and bills",
  "CSV and OFX import with a preview before anything is saved",
  "Reports by category",
  "Shared household organization with roles",
  "EU-hosted, export anytime, never used to train AI",
  "Optional AI: bring your own key or run it locally with Ollama, with spend caps and an audit trail",
];

const softwareLd = {
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  name: siteName,
  applicationCategory: "FinanceApplication",
  operatingSystem: "Web",
  url: apexCanonical("/features"),
  author: { "@id": orgId },
  publisher: { "@id": orgId },
  featureList: shippedFeatures,
};
const breadcrumbLd = {
  "@context": "https://schema.org",
  "@type": "BreadcrumbList",
  itemListElement: [
    { "@type": "ListItem", position: 1, name: "Home", item: apexCanonical("/") },
    { "@type": "ListItem", position: 2, name: "Features", item: apexCanonical("/features") },
  ],
};

// Visible on-page FAQ (Google requires FAQPage Q&A to be visible). The same
// array drives both the rendered <details> list below and the FAQPage JSON-LD,
// so the structured data can't drift from what visitors read.
const featuresFaq: ReadonlyArray<{ readonly q: string; readonly a: string }> = [
  {
    q: "Does The Better Decision forecast my cash flow?",
    a: "Yes. It projects a forecast end-of-month balance for every account from your recurring income and bills, your category budgets, and what-if adjustments, so you can see whether the rest of the month still works.",
  },
  {
    q: "Do I have to connect my bank account?",
    a: "No. You import transactions from your bank by CSV or OFX, with a preview before anything is saved. There is no bank-linking requirement.",
  },
  {
    q: "Is my financial data private?",
    a: "Yes. Your data is hosted in the EU and processed under EU law, you can export it anytime, and it is never sold and never used to train AI.",
  },
  {
    q: "Is The Better Decision free?",
    a: "Yes, it is free during the beta.",
  },
  {
    q: "Can my partner or household use it together?",
    a: "Yes. Finances are organized per household, so several people can share one organization with clear roles and boundaries.",
  },
  {
    q: "Does it use AI, and is that optional?",
    a: "AI is optional and opt-in. Bring your own OpenAI or Anthropic key, or run it locally with Ollama. It suggests categories and refines forecasts, you approve every suggestion before anything is saved, and there are hard spend caps plus a full audit trail.",
  },
];

const faqLd = {
  "@context": "https://schema.org",
  "@type": "FAQPage",
  mainEntity: featuresFaq.map((entry) => ({
    "@type": "Question",
    name: entry.q,
    acceptedAnswer: {
      "@type": "Answer",
      text: entry.a,
    },
  })),
};
const structuredData = [softwareLd, breadcrumbLd, faqLd];

const groups = [
  {
    title: "See clearly",
    points: [
      "Auto-categorization that learns from your edits.",
      "Import CSV or OFX from your bank, with a preview before anything is written.",
      "Reports that group spending by category.",
    ],
  },
  {
    title: "Plan what is coming",
    points: [
      "Forecast plans with what-if scenarios and a projected end-of-month balance.",
      "Recurring income and bills that roll forward automatically.",
      "Category budgets for the current period.",
    ],
  },
  {
    title: "Together, if you want",
    points: ["One organization, multiple people, clear roles and boundaries."],
  },
  {
    title: "Yours",
    points: [
      "EU-hosted and processed under EU law.",
      "Export your data anytime. It is never sold and never used to train AI.",
      "Delete your account, and your data, in one click.",
    ],
  },
  {
    title: "AI on your terms",
    points: [
      "Suggest a category, refine a forecast with seasonal patterns, or rebalance a budget. You accept or reject every suggestion before anything is saved.",
      "Bring your own OpenAI or Anthropic key, or run it entirely locally with Ollama.",
      "Hard and soft spend caps, plus a full audit trail of every call.",
    ],
  },
];

export default async function FeaturesPage() {
  const nonce = await readNonce();
  const nonceProp = nonce ? { nonce } : {};
  return (
    <MarketingShell>
    <main className="mx-auto max-w-4xl px-6 py-20 lg:py-24">
      {structuredData.map((block) => (
        <script
          key={block["@type"]}
          type="application/ld+json"
          {...nonceProp}
          dangerouslySetInnerHTML={{
            __html: JSON.stringify(block).replace(/<\/script>/gi, "<\\/script>"),
          }}
        />
      ))}
      <h1 className="font-display text-3xl font-semibold leading-tight text-text-primary lg:text-4xl">
        Everything in The Better Decision
      </h1>
      <p className="mt-4 max-w-2xl text-base leading-relaxed text-text-secondary">
        Budgeting and cash-flow forecasting in one calm app, EU-hosted, for normal
        people. Here is what it does today.
      </p>

      <div className="mt-12 grid gap-6 md:grid-cols-2">
        {groups.map((g) => (
          <section key={g.title} className="rounded-xl border border-border bg-surface p-6">
            <h2 className="font-display text-lg font-semibold text-text-primary">{g.title}</h2>
            <ul className="mt-3 list-disc space-y-2 pl-5 text-sm leading-relaxed text-text-secondary">
              {g.points.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          </section>
        ))}
      </div>

      <section
        aria-label="On the roadmap"
        className="mt-12 rounded-xl border border-dashed border-border bg-surface p-6"
      >
        <p className="text-xs font-semibold uppercase tracking-[0.12em] text-text-muted">
          On the roadmap
        </p>
        <h2 className="mt-2 font-display text-lg font-semibold text-text-primary">
          Not built yet, but coming
        </h2>
        <ul className="mt-3 list-disc space-y-2 pl-5 text-sm leading-relaxed text-text-secondary">
          <li>
            Connect your finances to your own AI assistant over MCP, with every change
            confirmed before it runs.
          </li>
          <li>A hosted AI option, opt-in and consent-gated, as an alternative to your own key.</li>
        </ul>
      </section>

      <section aria-label="Frequently asked questions" className="mt-12">
        <h2 className="font-display text-xl font-semibold text-text-primary">
          Frequently asked questions
        </h2>
        <ul className="mt-4 space-y-3">
          {featuresFaq.map((item) => (
            <li key={item.q} className="rounded-xl border border-border bg-surface">
              <details className="group">
                <summary className="flex cursor-pointer list-none items-center justify-between gap-4 rounded-xl px-5 py-4 text-left text-sm font-medium text-text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40">
                  <span>{item.q}</span>
                </summary>
                <div className="border-t border-border px-5 py-4 text-sm leading-relaxed text-text-secondary">
                  {item.a}
                </div>
              </details>
            </li>
          ))}
        </ul>
      </section>

      <p className="mt-12 text-sm text-text-muted">
        Wondering how it stacks up?{" "}
        <Link href="/compare" className="underline hover:text-text-primary">
          Compare The Better Decision with YNAB, PocketSmith, Monarch, and spreadsheets
        </Link>
        .
      </p>
    </main>
    </MarketingShell>
  );
}
