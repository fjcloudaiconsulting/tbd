// frontend/app/compare/page.tsx
import Link from "next/link";
import type { Metadata } from "next";
import { readNonce } from "@/lib/nonce";
import { apexCanonical, pageSocialMeta, siteName } from "@/lib/site";
import ComparisonTable from "@/components/landing/ComparisonTable";
import MarketingShell from "@/components/landing/MarketingShell";
import { competitorOrder } from "@/lib/comparison";

const description =
  "Compare The Better Decision with YNAB, PocketSmith, Monarch, and spreadsheets on forecasting, budgeting, bank sync, EU data residency, and price.";

export const metadata: Metadata = {
  title: "Compare budgeting and forecasting apps",
  description,
  alternates: { canonical: apexCanonical("/compare") },
  ...pageSocialMeta({
    title: `Compare budgeting and forecasting apps · ${siteName}`,
    description,
    path: apexCanonical("/compare"),
  }),
  robots: { index: true, follow: true },
};

const breadcrumbLd = {
  "@context": "https://schema.org",
  "@type": "BreadcrumbList",
  itemListElement: [
    { "@type": "ListItem", position: 1, name: "Home", item: apexCanonical("/") },
    { "@type": "ListItem", position: 2, name: "Compare", item: apexCanonical("/compare") },
  ],
};
const faqLd = {
  "@context": "https://schema.org",
  "@type": "FAQPage",
  mainEntity: [
    {
      "@type": "Question",
      name: "What is the best app for budgeting and cash-flow forecasting?",
      acceptedAnswer: {
        "@type": "Answer",
        text: "It depends on what you need. The Better Decision combines budgeting with forward-looking cash-flow forecasting, EU-hosted, and imports CSV or OFX rather than linking your bank. YNAB is strongest for strict envelope budgeting, PocketSmith for deep long-range forecasting, and Monarch for live bank and investment aggregation.",
      },
    },
    {
      "@type": "Question",
      name: "Which budgeting apps host data in the EU?",
      acceptedAnswer: {
        "@type": "Answer",
        text: "The Better Decision is EU-hosted and processed under EU law. PocketSmith is based in New Zealand, which has an EU adequacy decision. YNAB and Monarch are US-based.",
      },
    },
  ],
};
const structuredData = [breadcrumbLd, faqLd];

export default async function ComparePage() {
  const nonce = await readNonce();
  const nonceProp = nonce ? { nonce } : {};
  return (
    <MarketingShell>
    <main className="mx-auto max-w-5xl px-6 py-20 lg:py-24">
      {structuredData.map((block) => (
        <script
          key={(block as { "@type": string })["@type"]}
          type="application/ld+json"
          {...nonceProp}
          dangerouslySetInnerHTML={{
            __html: JSON.stringify(block).replace(/<\/script>/gi, "<\\/script>"),
          }}
        />
      ))}
      <h1 className="font-display text-3xl font-semibold leading-tight text-text-primary lg:text-4xl">
        How The Better Decision compares
      </h1>
      <p className="mt-4 max-w-2xl text-base leading-relaxed text-text-secondary">
        The Better Decision is a budgeting and cash-flow forecasting app, EU-hosted,
        that imports your CSV or OFX files instead of linking your bank. Here is how it
        stacks up against the tools people switch from. No tool wins every row, and the
        table says so.
      </p>
      <div className="mt-10">
        <ComparisonTable competitors={competitorOrder} />
      </div>
      <p className="mt-10 text-sm text-text-muted">
        Read the detailed comparisons:{" "}
        <Link href="/vs/spreadsheets" className="underline hover:text-text-primary">
          vs spreadsheets
        </Link>{" "}
        ·{" "}
        <Link href="/vs/ynab" className="underline hover:text-text-primary">
          vs YNAB
        </Link>
        .
      </p>
    </main>
    </MarketingShell>
  );
}
