// Treat empty string as unset — NEXT_PUBLIC_SITE_URL= in .env or a blank
// App Platform value would otherwise produce new URL("") and crash the build.
const rawSiteUrl = (process.env.NEXT_PUBLIC_SITE_URL || "").trim();
export const siteUrl = (rawSiteUrl || "https://app.thebetterdecision.com").replace(/\/$/, "");

export const siteName = "The Better Decision";

export const siteTagline = "know your money, plan what's next";

export const siteDescription =
  "A finance app for normal people. Know what you have, what's coming, and where it goes. No spreadsheet fatigue.";

// Next.js does NOT deep-merge openGraph/twitter across segments — any child
// that specifies these objects replaces the parent's wholesale. So each page
// must declare the full social shape (type, siteName, locale, images, card).
// This helper keeps the shape in one place.
const ogImage = {
  url: "/opengraph-image",
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
