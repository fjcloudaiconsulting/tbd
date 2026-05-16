import type { NextConfig } from "next";

import { securityHeaders } from "./lib/security-headers";

// The app-host root → /login redirect previously lived here in
// ``redirects()``. ZAP scan 2026-05-16 surfaced that Next.js's
// ``redirects()`` short-circuits before ``headers()`` applies, so the
// 307 response was emitted without the security pack (no HSTS, no
// nosniff, no Referrer-Policy, etc.). The redirect was moved into
// ``frontend/proxy.ts`` where it stamps the headers on the response
// directly. See PR #292 + the May 16 entry in
// memory/audit_zap_2026_05_14.md.

const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  logging: {
    fetches: {
      fullUrl: false,
    },
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: securityHeaders,
      },
    ];
  },
};

export default nextConfig;
