import type { Metadata } from "next";
import LandingPageBody from "@/components/landing/LandingPageBody";
import { siteDescription, siteName, siteTagline, siteUrl } from "@/lib/site";

const pageTitle = `${siteName}: ${siteTagline}`;

export const metadata: Metadata = {
  title: {
    absolute: pageTitle,
  },
  description: siteDescription,
  alternates: {
    canonical: "/",
  },
  openGraph: {
    url: "/",
    title: pageTitle,
    description: siteDescription,
  },
  twitter: {
    card: "summary_large_image",
    title: pageTitle,
    description: siteDescription,
  },
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

export default function LandingPage() {
  return (
    <>
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(jsonLd) }}
      />
      <LandingPageBody />
    </>
  );
}
