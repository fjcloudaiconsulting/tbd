import { describe, it, expect } from "vitest";
import type { Metadata } from "next";

// forgot-password, reset-password, verify-email, mfa-verify have no
// export const metadata — they inherit root's noindex by default.
// Only onboarding, accept-invite and signup/complete export metadata; none may
// opt back into index.
import { metadata as onboardingMetadata } from "@/app/onboarding/page";
import { metadata as acceptInviteMetadata } from "@/app/accept-invite/page";
import { metadata as signupCompleteMetadata } from "@/app/signup/complete/page";
import robots from "@/app/robots";

// These pages should NOT opt back into index — they inherit root's noindex.
describe("flow-only public routes are not indexed", () => {
  const flowMetadatas: ReadonlyArray<[string, Metadata]> = [
    ["/onboarding", onboardingMetadata],
    ["/accept-invite", acceptInviteMetadata],
    ["/signup/complete", signupCompleteMetadata],
  ] as const;

  it.each(flowMetadatas)("%s does not opt back into index", (_route, meta) => {
    if (meta.robots !== undefined) {
      // Page declared its own robots — it MUST be noindex.
      const r = meta.robots as { index?: boolean; follow?: boolean };
      expect(r.index).toBe(false);
    }
    // No explicit robots = inherits root noindex (default).
    // The root-layout guarantee is verified centrally in
    // tests/root-layout-robots-default.test.tsx — no need to duplicate here.
  });
});

// TBD-654: the emailed sign-up link page is noindex AND disallowed.
describe("/signup/complete", () => {
  it("declares noindex", () => {
    expect(signupCompleteMetadata.robots).toEqual({ index: false, follow: false });
  });

  it("is disallowed in robots.txt", () => {
    const rules = robots().rules;
    const disallow = (Array.isArray(rules) ? rules[0] : rules).disallow;
    expect(disallow).toContain("/signup");
  });
});
