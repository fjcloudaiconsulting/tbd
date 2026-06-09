// frontend/app/vs/monarch/page.tsx
// NOTE: noindex until the publish PR (staggered launch).
import type { Metadata } from "next";
import { readNonce } from "@/lib/nonce";
import { apexCanonical, pageSocialMeta, siteName } from "@/lib/site";
import VsPageLayout from "@/components/landing/VsPageLayout";

const description =
  "The Better Decision leads with planning and EU data residency, with no bank linking required. Here is the honest comparison with Monarch.";

export const metadata: Metadata = {
  title: "The Better Decision vs Monarch",
  description,
  alternates: { canonical: apexCanonical("/vs/monarch") },
  ...pageSocialMeta({
    title: `vs Monarch · ${siteName}`,
    description,
    path: apexCanonical("/vs/monarch"),
  }),
  robots: { index: false, follow: false },
};

const faq = [
  {
    q: "Is The Better Decision a Monarch alternative?",
    a: "Yes, if you want forecasting-first planning and EU data residency, and you prefer not to auto-link every account. Monarch's strength is comprehensive live bank and investment aggregation, which The Better Decision does not do.",
  },
  {
    q: "Does it track net worth like Monarch?",
    a: "Not in the same automated way. Monarch links accounts to track net worth. The Better Decision imports CSV or OFX and focuses on cash-flow planning.",
  },
  {
    q: "Where is my data stored?",
    a: "In the EU, processed under EU law. Monarch is US-based.",
  },
];

export default async function VsMonarchPage() {
  const nonce = await readNonce();
  return (
    <VsPageLayout
      slug="monarch"
      competitor="monarch"
      title="The Better Decision vs Monarch"
      faq={faq}
      nonce={nonce}
      intro={
        <>
          <p>
            Monarch is a polished US tracking and net-worth app built on bank aggregation. The
            Better Decision leads with planning: a projected balance and what-if scenarios,
            EU-hosted, with no account linking required.
          </p>
          <p>
            Monarch's automatic aggregation is genuinely strong, and this comparison concedes it.
          </p>
        </>
      }
    />
  );
}
