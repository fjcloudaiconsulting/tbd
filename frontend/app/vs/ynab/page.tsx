// frontend/app/vs/ynab/page.tsx
import type { Metadata } from "next";
import { readNonce } from "@/lib/nonce";
import { apexCanonical, pageSocialMeta, siteName } from "@/lib/site";
import VsPageLayout from "@/components/landing/VsPageLayout";

const description =
  "A YNAB alternative built around forecasting and EU data residency. The Better Decision adds the forward view YNAB leaves out, and concedes where YNAB still wins.";

export const metadata: Metadata = {
  title: "A YNAB alternative built around forecasting",
  description,
  alternates: { canonical: apexCanonical("/vs/ynab") },
  ...pageSocialMeta({
    title: `vs YNAB · ${siteName}`,
    description,
    path: apexCanonical("/vs/ynab"),
  }),
  robots: { index: true, follow: true },
};

const faq = [
  {
    q: "Is The Better Decision a good YNAB alternative?",
    a: "Yes, if you want a forward-looking projected balance and EU data residency. It adds cash-flow forecasting that YNAB does not do. It does not have YNAB's live bank sync.",
  },
  {
    q: "Does it use the envelope method like YNAB?",
    a: "No. YNAB is built around zero-based envelopes for the current month. The Better Decision focuses on category budgets plus a forward forecast across periods.",
  },
  {
    q: "Does it sync with my bank like YNAB?",
    a: "No. The Better Decision imports CSV or OFX files. Nothing connects to your bank automatically, which some people prefer.",
  },
];

export default async function VsYnabPage() {
  const nonce = await readNonce();
  return (
    <VsPageLayout
      slug="ynab"
      competitor="ynab"
      title="The Better Decision vs YNAB"
      faq={faq}
      nonce={nonce}
      intro={
        <>
          <p>
            Looking for a YNAB alternative? YNAB is a zero-based budgeting method where every
            dollar gets a job, this month. The Better Decision adds the forward view YNAB
            deliberately leaves out: a projected balance across your accounts weeks and months
            ahead, with what-if scenarios, EU-hosted.
          </p>
          <p>
            YNAB is a strong product with a loyal community, so this comparison is fair about
            where it still wins.
          </p>
        </>
      }
    />
  );
}
