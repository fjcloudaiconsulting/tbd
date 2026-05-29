import { describe, it, expect } from "vitest";
import type { Metadata } from "next";

// forgot-password, reset-password, verify-email, mfa-verify have no
// export const metadata — they inherit root's noindex by default.
// Only onboarding and accept-invite export metadata; both must not
// opt back into index.
import { metadata as onboardingMetadata } from "@/app/onboarding/page";
import { metadata as acceptInviteMetadata } from "@/app/accept-invite/page";

// These pages should NOT opt back into index — they inherit root's noindex.
describe("flow-only public routes are not indexed", () => {
  const flowMetadatas: ReadonlyArray<[string, Metadata]> = [
    ["/onboarding", onboardingMetadata],
    ["/accept-invite", acceptInviteMetadata],
  ] as const;

  it.each(flowMetadatas)("%s does not opt back into index", (_route, meta) => {
    // Either no robots key (inherit root noindex) or explicit noindex.
    if (meta.robots !== undefined) {
      expect(meta.robots).toEqual({ index: false, follow: false });
    }
  });
});
