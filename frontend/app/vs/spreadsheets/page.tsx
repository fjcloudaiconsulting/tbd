// frontend/app/vs/spreadsheets/page.tsx
import type { Metadata } from "next";
import { readNonce } from "@/lib/nonce";
import { apexCanonical, pageSocialMeta, siteName } from "@/lib/site";
import VsPageLayout from "@/components/landing/VsPageLayout";

const description =
  "Budget without spreadsheets. The Better Decision forecasts your cash flow automatically, with no formulas to maintain. Here is the honest comparison.";

export const metadata: Metadata = {
  title: "The Better Decision vs spreadsheets: budget without a spreadsheet",
  description,
  alternates: { canonical: apexCanonical("/vs/spreadsheets") },
  ...pageSocialMeta({
    title: `vs Spreadsheets · ${siteName}`,
    description,
    path: apexCanonical("/vs/spreadsheets"),
  }),
  robots: { index: true, follow: true },
};

const faq = [
  {
    q: "Can The Better Decision replace my budgeting spreadsheet?",
    a: "For most people, yes. It imports your CSV or OFX, categorizes transactions, and forecasts your end-of-month balance automatically, without formulas to maintain.",
  },
  {
    q: "Do I lose the flexibility of a spreadsheet?",
    a: "You trade some flexibility for automation. A spreadsheet can model anything you can express in a formula. If you have an unusual calculation, a spreadsheet still wins.",
  },
  {
    q: "Is it free?",
    a: "It is free while in beta. A spreadsheet is free forever, which the comparison says plainly.",
  },
];

export default async function VsSpreadsheetsPage() {
  const nonce = await readNonce();
  return (
    <VsPageLayout
      slug="spreadsheets"
      competitor="spreadsheets"
      title="The Better Decision vs spreadsheets"
      faq={faq}
      nonce={nonce}
      intro={
        <>
          <p>
            A spreadsheet shows you what you typed. The Better Decision shows you what is
            coming: recurring bills and income roll forward automatically into a projected
            end-of-month balance, and imported transactions reconcile against your plan
            without you maintaining a single formula.
          </p>
          <p>
            If you are looking to budget without a spreadsheet but keep the control, this is
            the honest trade-off, including where a spreadsheet still wins.
          </p>
        </>
      }
    />
  );
}
