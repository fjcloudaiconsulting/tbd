import type { Metadata } from "next";
import Faq from "@/components/landing/Faq";
import { faqEntries } from "@/components/landing/faqData";
import FeatureTiles from "@/components/landing/FeatureTiles";
import Hero from "@/components/landing/Hero";
import HowItWorks from "@/components/landing/HowItWorks";
import LandingAuthRedirect from "@/components/landing/LandingAuthRedirect";
import LandingFooter from "@/components/landing/LandingFooter";
import ScreenshotShowcase from "@/components/landing/ScreenshotShowcase";
import SecondCta from "@/components/landing/SecondCta";
import TopNav from "@/components/landing/TopNav";
import { readNonce } from "@/lib/nonce";
import {
  pageSocialMeta,
  siteDescription,
  siteName,
  siteTagline,
  siteUrl,
} from "@/lib/site";

const pageTitle = `${siteName}: ${siteTagline}`;

export const metadata: Metadata = {
  title: {
    absolute: pageTitle,
  },
  description: siteDescription,
  alternates: {
    canonical: "/",
  },
  ...pageSocialMeta({
    title: pageTitle,
    description: siteDescription,
    path: "/",
  }),
  robots: { index: true, follow: true },
};

const jsonLd = {
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  name: siteName,
  description: siteDescription,
  applicationCategory: "FinanceApplication",
  operatingSystem: "Web",
  url: siteUrl,
  author: { "@type": "Organization", name: siteName, url: siteUrl },
  publisher: { "@type": "Organization", name: siteName, url: siteUrl },
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
      <script
        type="application/ld+json"
        {...nonceProp}
        dangerouslySetInnerHTML={{
          __html: JSON.stringify(jsonLd).replace(/<\/script>/gi, '<\\/script>'),
        }}
      />
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
