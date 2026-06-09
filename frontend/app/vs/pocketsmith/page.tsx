// frontend/app/vs/pocketsmith/page.tsx
// NOTE: noindex until the publish PR (staggered launch). Do NOT add to sitemap,
// llms.txt, or internal links until then. Honesty: PocketSmith's headline is
// forecasting and it is NZ (EU adequacy), so this page leads with simplicity and
// household sharing, NOT "we forecast better" or a hard EU-privacy claim.
import type { Metadata } from "next";
import { readNonce } from "@/lib/nonce";
import { apexCanonical, pageSocialMeta, siteName } from "@/lib/site";
import VsPageLayout from "@/components/landing/VsPageLayout";

const description =
  "The Better Decision brings cash-flow forecasting into a simpler, calmer, household-shared app. Here is the honest comparison with PocketSmith.";

export const metadata: Metadata = {
  title: "The Better Decision vs PocketSmith",
  description,
  alternates: { canonical: apexCanonical("/vs/pocketsmith") },
  ...pageSocialMeta({
    title: `vs PocketSmith · ${siteName}`,
    description,
    path: apexCanonical("/vs/pocketsmith"),
  }),
  robots: { index: false, follow: false },
};

const faq = [
  {
    q: "Does PocketSmith forecast better than The Better Decision?",
    a: "PocketSmith offers deeper, longer-range forecasting with more scenarios and multi-currency. The Better Decision brings the core forward view into a simpler, calmer app.",
  },
  {
    q: "Is The Better Decision more private than PocketSmith?",
    a: "Both treat your data seriously. The Better Decision is EU-hosted and processed under EU law; PocketSmith is based in New Zealand, which has an EU adequacy decision. The difference is locality preference, not safety.",
  },
  {
    q: "Which is simpler to use?",
    a: "The Better Decision is designed for normal people and households, with less setup than PocketSmith's power-user depth.",
  },
];

export default async function VsPocketsmithPage() {
  const nonce = await readNonce();
  return (
    <VsPageLayout
      slug="pocketsmith"
      competitor="pocketsmith"
      title="The Better Decision vs PocketSmith"
      faq={faq}
      nonce={nonce}
      intro={
        <>
          <p>
            PocketSmith pioneered long-range cash-flow forecasting. The Better Decision brings
            that same forward view into a simpler, calmer, household-shared app, without
            spreadsheet-grade complexity, and hosts your data in the EU.
          </p>
          <p>
            PocketSmith forecasts deeper than we do today, and this comparison says so plainly.
          </p>
        </>
      }
    />
  );
}
