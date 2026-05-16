import { describe, expect, it } from "vitest";

import nextConfig from "@/next.config";

// next-config-redirects.test.ts — pins that the app-host root → /login
// redirect is NOT defined in next.config.ts's redirects(). It was moved
// to frontend/proxy.ts so the 307 response can carry the security
// header pack (HSTS, nosniff, Referrer-Policy, X-Frame-Options,
// Permissions-Policy). ZAP scan 2026-05-16 surfaced that next.config
// redirects() short-circuits before headers() applies.
//
// Behavior test for the redirect itself + headers stamping lives in
// frontend/tests/proxy-app-host-redirect.test.ts.

describe("next.config.ts redirects", () => {
  it("does NOT redirect the app-host root from next.config (moved to proxy.ts)", async () => {
    if (typeof nextConfig.redirects !== "function") {
      // No redirects() at all is the desired state.
      return;
    }
    const rules = await nextConfig.redirects();
    const appHostRule = rules.find(
      (r) =>
        r.source === "/" &&
        Array.isArray(r.has) &&
        r.has.some(
          (h) =>
            h.type === "host" && h.value === "app.thebetterdecision.com",
        ),
    );
    expect(
      appHostRule,
      "app-host root redirect must live in proxy.ts, not next.config.ts " +
        "(see PR #292 / ZAP scan 2026-05-16 / proxy-app-host-redirect.test.ts)",
    ).toBeUndefined();
  });
});
