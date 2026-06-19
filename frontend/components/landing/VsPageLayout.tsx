// frontend/components/landing/VsPageLayout.tsx
// Shared skeleton for every /vs/<x> page. The page passes its DISTINCT content
// (title, intro JSX, FAQ) plus the competitor slug; this layout pulls the table
// slice and the honest "where they win" points from the single comparison data
// source and assembles JSON-LD. Pages stay structurally consistent; their
// content stays distinct (so the cluster is not thin/doorway).
import Link from "next/link";
import type { ReactNode } from "react";
import ComparisonTable from "./ComparisonTable";
import MarketingShell from "./MarketingShell";
import { type Competitor, competitorMeta } from "@/lib/comparison";
import { btnPrimary } from "@/lib/styles";
import { signupHref } from "@/lib/links";
import { apexCanonical, apexUrl, siteName } from "@/lib/site";

const orgId = `${apexUrl}/#organization`;

export default function VsPageLayout({
  slug,
  competitor,
  title,
  intro,
  faq,
  nonce,
}: {
  slug: string;
  competitor: Exclude<Competitor, "tbd">;
  title: string;
  intro: ReactNode;
  faq: ReadonlyArray<{ q: string; a: string }>;
  nonce: string;
}) {
  const nonceProp = nonce ? { nonce } : {};
  const meta = competitorMeta[competitor];

  const faqLd = {
    "@context": "https://schema.org",
    "@type": "FAQPage",
    mainEntity: faq.map((f) => ({
      "@type": "Question",
      name: f.q,
      acceptedAnswer: { "@type": "Answer", text: f.a },
    })),
  };
  const breadcrumbLd = {
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    itemListElement: [
      { "@type": "ListItem", position: 1, name: "Home", item: apexCanonical("/") },
      { "@type": "ListItem", position: 2, name: "Compare", item: apexCanonical("/compare") },
      { "@type": "ListItem", position: 3, name: meta.name, item: apexCanonical(`/vs/${slug}`) },
    ],
  };
  const softwareLd = {
    "@context": "https://schema.org",
    "@type": "SoftwareApplication",
    name: siteName,
    applicationCategory: "FinanceApplication",
    operatingSystem: "Web",
    url: apexCanonical(`/vs/${slug}`),
    author: { "@id": orgId },
    publisher: { "@id": orgId },
    offers: { "@type": "Offer", price: "0", priceCurrency: "EUR" },
  };
  const structuredData = [faqLd, breadcrumbLd, softwareLd];

  return (
    <MarketingShell>
    <main className="mx-auto max-w-3xl px-6 py-20 lg:py-24">
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
        {title}
      </h1>
      <div className="mt-4 space-y-4 text-base leading-relaxed text-text-secondary">
        {intro}
      </div>

      <section aria-label="Feature comparison" className="mt-12">
        <ComparisonTable competitors={["tbd", competitor]} />
      </section>

      <section
        aria-label={`Where ${meta.name} wins`}
        className="mt-12 rounded-xl border border-border bg-surface p-6"
      >
        <h2 className="font-display text-xl font-semibold text-text-primary">
          Where {meta.name} wins
        </h2>
        <p className="mt-2 text-sm text-text-muted">
          No tool wins every row. Here is where {meta.name} is the better choice.
        </p>
        <ul className="mt-4 list-disc space-y-2 pl-5 text-sm leading-relaxed text-text-secondary">
          {meta.whereTheyWin.map((point) => (
            <li key={point}>{point}</li>
          ))}
        </ul>
      </section>

      <section aria-label="Common questions" className="mt-12">
        <h2 className="font-display text-xl font-semibold text-text-primary">
          Common questions
        </h2>
        <ul className="mt-4 space-y-3">
          {faq.map((item) => (
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

      <section className="mt-12 text-center">
        <p className="text-base text-text-secondary">
          See your money clearly and plan what is coming. Free while in beta.
        </p>
        <Link href={signupHref()} className={`${btnPrimary} mt-4 inline-flex items-center`}>
          Get started
        </Link>
        <p className="mt-6 text-sm text-text-muted">
          <Link href="/compare" className="underline hover:text-text-primary">
            Compare all options
          </Link>{" "}
          ·{" "}
          <Link href="/features" className="underline hover:text-text-primary">
            See every feature
          </Link>
        </p>
      </section>
    </main>
    </MarketingShell>
  );
}
