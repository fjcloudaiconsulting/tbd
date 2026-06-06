import type { Metadata } from "next";
import AnswerLead from "@/components/landing/AnswerLead";
import Faq from "@/components/landing/Faq";
import { faqEntries } from "@/components/landing/faqData";
import FeatureTiles from "@/components/landing/FeatureTiles";
import Hero from "@/components/landing/Hero";
import HowItWorks from "@/components/landing/HowItWorks";
import { howItWorksSteps } from "@/components/landing/howItWorksData";
import LandingAuthRedirect from "@/components/landing/LandingAuthRedirect";
import LandingFooter from "@/components/landing/LandingFooter";
import ScreenshotShowcase from "@/components/landing/ScreenshotShowcase";
import SecondCta from "@/components/landing/SecondCta";
import TopNav from "@/components/landing/TopNav";
import { readNonce } from "@/lib/nonce";
import {
  apexCanonical,
  apexUrl,
  pageSocialMeta,
  siteDescription,
  siteName,
  siteTagline,
} from "@/lib/site";

const pageTitle = `${siteName}: ${siteTagline}`;
const apexHome = `${apexUrl}/`;

export const metadata: Metadata = {
  title: {
    absolute: pageTitle,
  },
  description: siteDescription,
  alternates: {
    // Canonicalize to the apex marketing host. The app-subdomain render of
    // "/" redirects to /login, so this canonical only ships on the apex.
    canonical: apexCanonical("/"),
  },
  ...pageSocialMeta({
    title: pageTitle,
    description: siteDescription,
    path: apexCanonical("/"),
  }),
  robots: { index: true, follow: true },
};

// Structured data for Google rich results and AI-engine entity resolution.
// Emitted as separate top-level blocks (not a single @graph) and linked by
// @id so the SoftwareApplication / WebSite reference one canonical
// Organization node. All URLs point at the apex so the entity resolves to
// the single canonical home regardless of which host served the page.
const orgId = `${apexUrl}/#organization`;
const websiteId = `${apexUrl}/#website`;

const organizationLd = {
  "@context": "https://schema.org",
  "@type": "Organization",
  "@id": orgId,
  name: siteName,
  url: apexHome,
  logo: {
    "@type": "ImageObject",
    url: `${apexUrl}/icon.svg`,
  },
  description: siteDescription,
};

const websiteLd = {
  "@context": "https://schema.org",
  "@type": "WebSite",
  "@id": websiteId,
  name: siteName,
  url: apexHome,
  inLanguage: "en-US",
  publisher: { "@id": orgId },
};

const softwareApplicationLd = {
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  name: siteName,
  description: siteDescription,
  applicationCategory: "FinanceApplication",
  operatingSystem: "Web",
  url: apexHome,
  author: { "@id": orgId },
  publisher: { "@id": orgId },
  offers: {
    "@type": "Offer",
    price: "0",
    priceCurrency: "EUR",
    availability: "https://schema.org/InStock",
    // Trial copy hidden pending payment platform launch. Restore the
    // "14-day free trial" description when BILLING_UI_ENABLED flips
    // to true (Option A from specs/2026-05-21-hide-billing-ui-until-payment.md).
  },
};

const howToLd = {
  "@context": "https://schema.org",
  "@type": "HowTo",
  name: "How to get started with The Better Decision",
  description:
    "Set up The Better Decision and reach the first calm view of your money in about thirty minutes.",
  totalTime: "PT30M",
  step: howItWorksSteps.map((step, i) => ({
    "@type": "HowToStep",
    position: i + 1,
    name: step.title,
    text: step.body,
  })),
};

// Order: entity (Organization) first, then the site, app, and how-to that
// reference it. FAQPage is rendered as its own block below.
const structuredData = [
  organizationLd,
  websiteLd,
  softwareApplicationLd,
  howToLd,
];

// Server component — renders the landing content in the initial HTML so
// crawlers and no-JS visitors receive it directly. LandingAuthRedirect
// is a client island that redirects authenticated visitors to /dashboard
// (or /setup) after hydration.
export default async function LandingPage() {
  // Read the per-request nonce so the JSON-LD inline script passes
  // the strict prod CSP. ``script-src`` drops ``'unsafe-inline'`` in
  // production; without an explicit nonce the browser refuses to
  // parse this block. ``readNonce`` returns ``""`` on the apex static
  // export (no request context), so we conditionally spread the prop
  // — same pattern app/layout.tsx uses.
  const nonce = await readNonce();
  const nonceProp = nonce ? { nonce } : {};
  return (
    <>
      {structuredData.map((block) => (
        <script
          key={block["@type"]}
          type="application/ld+json"
          {...nonceProp}
          dangerouslySetInnerHTML={{
            __html: JSON.stringify(block).replace(/<\/script>/gi, '<\\/script>'),
          }}
        />
      ))}
      <script
        type="application/ld+json"
        {...nonceProp}
        dangerouslySetInnerHTML={{
          __html: JSON.stringify({
            "@context": "https://schema.org",
            "@type": "FAQPage",
            mainEntity: faqEntries.map((entry) => ({
              "@type": "Question",
              name: entry.q,
              acceptedAnswer: {
                "@type": "Answer",
                text: entry.a,
              },
            })),
          }).replace(/<\/script>/gi, '<\\/script>'),
        }}
      />
      <LandingAuthRedirect />
      <div className="min-h-screen bg-bg text-text-primary">
        <TopNav />
        <main>
          <Hero />
          <AnswerLead />
          <FeatureTiles />
          <ScreenshotShowcase />
          <HowItWorks />
          <Faq />
          <SecondCta />
        </main>
        <LandingFooter />
      </div>
    </>
  );
}
