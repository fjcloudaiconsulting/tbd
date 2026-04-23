import type { Metadata } from "next";
import FeatureTiles from "@/components/landing/FeatureTiles";
import Hero from "@/components/landing/Hero";
import LandingAuthRedirect from "@/components/landing/LandingAuthRedirect";
import LandingFooter from "@/components/landing/LandingFooter";
import SecondCta from "@/components/landing/SecondCta";
import TopNav from "@/components/landing/TopNav";
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
};

const jsonLd = {
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  name: siteName,
  description: siteDescription,
  applicationCategory: "FinanceApplication",
  operatingSystem: "Web",
  url: siteUrl,
  offers: {
    "@type": "Offer",
    price: "0",
    priceCurrency: "EUR",
    availability: "https://schema.org/InStock",
    description: "14-day free trial",
  },
};

// Server component — renders the landing content in the initial HTML so
// crawlers and no-JS visitors receive it directly. LandingAuthRedirect
// is a client island that redirects authenticated visitors to /dashboard
// (or /setup) after hydration.
export default function LandingPage() {
  return (
    <>
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(jsonLd) }}
      />
      <LandingAuthRedirect />
      <div className="min-h-screen bg-bg text-text-primary">
        <TopNav />
        <main>
          <Hero />
          <FeatureTiles />
          <SecondCta />
        </main>
        <LandingFooter />
      </div>
    </>
  );
}
