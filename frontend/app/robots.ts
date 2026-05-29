import type { MetadataRoute } from "next";
import { siteUrl } from "@/lib/site";

export default function robots(): MetadataRoute.Robots {
  return {
    rules: [
      {
        userAgent: "*",
        allow: ["/", "/login", "/register", "/privacy", "/terms", "/docs", "/docs/plans"],
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
