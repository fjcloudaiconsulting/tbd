// Treat empty string as unset — NEXT_PUBLIC_SITE_URL= in .env or a blank
// App Platform value would otherwise produce new URL("") and crash the build.
const rawSiteUrl = (process.env.NEXT_PUBLIC_SITE_URL || "").trim();
export const siteUrl = (rawSiteUrl || "https://app.thebetterdecision.com").replace(/\/$/, "");

// The apex (marketing) host. This is the SINGLE canonical home for all
// shared public content. The same Next.js app is served two ways:
//   - apex static export on thebetterdecision.com (next.config.apex.ts)
//   - SSR app on app.thebetterdecision.com (next.config.ts)
// Pages that exist on BOTH hosts (/, /privacy, /terms, /docs, /docs/plans)
// must canonicalize to the apex so the app-subdomain copies don't split
// ranking signal. App-only pages (/login, /register) keep self-canonicals.
const rawApexUrl = (process.env.NEXT_PUBLIC_APEX_URL || "").trim();
export const apexUrl = (rawApexUrl || "https://thebetterdecision.com").replace(/\/$/, "");

// Absolute apex canonical URL for a shared public page. Includes the
// trailing slash so it matches the apex static export (trailingSlash: true);
// passing an absolute string makes Next.js emit it verbatim on BOTH build
// targets instead of resolving against the per-build metadataBase.
export function apexCanonical(path: string): string {
  const clean = path.replace(/^\/+|\/+$/g, "");
  return clean ? `${apexUrl}/${clean}/` : `${apexUrl}/`;
}

export const siteName = "The Better Decision";

export const siteTagline = "know your money, plan what's next";

export const siteDescription =
  "A finance app for normal people. Know what you have, what's coming, and where it goes. No spreadsheet fatigue.";

// Next.js does NOT deep-merge openGraph/twitter across segments — any child
// that specifies these objects replaces the parent's wholesale. So each page
// must declare the full social shape (type, siteName, locale, images, card).
// This helper keeps the shape in one place.
// Social-share image. Served as a static asset from public/og.png so it
// resolves on BOTH build targets. The dynamic next/og route at
// /opengraph-image is NOT exported by the apex static build (it would force
// a server runtime), so referencing it there produced a 404 on every
// unfurl. A committed static PNG works for the apex export and the SSR app
// alike. Keep this at 1200x630.
const ogImage = {
  url: "/og.png",
  width: 1200,
  height: 630,
  alt: `${siteName}: ${siteTagline}`,
};

export function pageSocialMeta({
  title,
  description,
  path,
}: {
  title: string;
  description: string;
  path: string;
}) {
  return {
    openGraph: {
      type: "website" as const,
      siteName,
      locale: "en_US",
      url: path,
      title,
      description,
      images: [ogImage],
    },
    twitter: {
      card: "summary_large_image" as const,
      title,
      description,
      images: [ogImage.url],
    },
  };
}
