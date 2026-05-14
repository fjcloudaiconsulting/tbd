import { describe, expect, it } from "vitest";

import nextConfig from "@/next.config";

// next-config-redirects.test.ts — guards the host-scoped root redirect
// that prevents app.thebetterdecision.com from serving the marketing
// landing page (which is hosted on the apex via S3 + CloudFront via
// next.config.apex.ts). If somebody removes or mutates the rule, this
// test fails loudly. See PR comments on the L5.2a follow-up hotfix.

describe("next.config.ts redirects", () => {
  it("redirects app-host root to /login with host-scoping", async () => {
    if (typeof nextConfig.redirects !== "function") {
      throw new Error("redirects() not configured");
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
    expect(appHostRule).toBeDefined();
    expect(appHostRule!.destination).toBe("/login");
    // 307, not 308 — flexible during early launch.
    expect(appHostRule!.permanent).toBe(false);
  });
});
