import type { MetadataRoute } from "next";
import { siteUrl } from "@/lib/site";

export default function sitemap(): MetadataRoute.Sitemap {
  const lastModified = new Date();

  return [
    {
      url: `${siteUrl}/`,
      lastModified,
      changeFrequency: "weekly",
      priority: 1,
    },
    {
      url: `${siteUrl}/register`,
      lastModified,
      changeFrequency: "monthly",
      priority: 0.9,
    },
    // /login is intentionally omitted: it is noindex (see app/login/page.tsx)
    // so it must not appear in the sitemap.
    {
      url: `${siteUrl}/privacy`,
      lastModified,
      changeFrequency: "yearly",
      priority: 0.3,
    },
    {
      url: `${siteUrl}/terms`,
      lastModified,
      changeFrequency: "yearly",
      priority: 0.3,
    },
    {
      url: `${siteUrl}/features`,
      lastModified,
      changeFrequency: "monthly",
      priority: 0.7,
    },
    {
      url: `${siteUrl}/compare`,
      lastModified,
      changeFrequency: "monthly",
      priority: 0.7,
    },
    // /vs/spreadsheets and /vs/ynab are indexable (robots index:true). The
    // other two /vs pages (pocketsmith, monarch) are noindex and stay out.
    {
      url: `${siteUrl}/vs/spreadsheets`,
      lastModified,
      changeFrequency: "monthly",
      priority: 0.6,
    },
    {
      url: `${siteUrl}/vs/ynab`,
      lastModified,
      changeFrequency: "monthly",
      priority: 0.6,
    },
    {
      url: `${siteUrl}/docs`,
      lastModified,
      changeFrequency: "monthly",
      priority: 0.4,
    },
    {
      url: `${siteUrl}/docs/plans`,
      lastModified,
      changeFrequency: "monthly",
      priority: 0.4,
    },
  ];
}
