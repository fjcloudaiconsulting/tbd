import type { MetadataRoute } from "next";
import { siteUrl } from "@/lib/site";

// Per-request: siteUrl comes from runtime env, so this must not be prerendered at build.
export const dynamic = "force-dynamic";

export default function robots(): MetadataRoute.Robots {
  return {
    rules: [
      {
        userAgent: "*",
        allow: ["/", "/login", "/register", "/privacy", "/terms", "/docs", "/docs/plans", "/features", "/compare", "/vs"],
        disallow: [
          "/dashboard",
          "/accounts",
          "/transactions",
          "/budgets",
          "/categories",
          "/forecast-plans",
          "/recurring",
          "/import",
          "/profile",
          "/settings",
          "/admin",
          "/system",
          "/setup",
          "/onboarding",
          "/accept-invite",
          "/forgot-password",
          "/verify-email",
          "/reset-password",
          "/mfa-verify",
          "/auth",
          "/api",
        ],
      },
    ],
    sitemap: `${siteUrl}/sitemap.xml`,
    host: siteUrl,
  };
}
