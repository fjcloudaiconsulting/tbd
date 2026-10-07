// INFRA-130: the privacy policy must name the recipients it really sends data to
// and the request-log window the platform enforces (7 days, aws-infra INFRA-130).
import React from "react";
import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import PrivacyPolicyPage from "@/app/privacy/page";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/privacy",
}));

const text = () => render(<PrivacyPolicyPage />).container.textContent ?? "";

describe("privacy policy recipients and retention", () => {
  it("lists Google Analytics and the bring-your-own-key AI provider in section 3", () => {
    const t = text();
    const s3 = t.slice(t.indexOf("3. Third parties"), t.indexOf("4. How long"));
    expect(s3).toContain("Google (Analytics");
    expect(s3).toMatch(/own AI provider with its own key/);
  });

  it("states request logs are kept up to 7 days, never 30", () => {
    const t = text();
    expect(t.match(/up to 7 days/g)?.length).toBe(2);
    expect(t).not.toMatch(/up to 30 days/);
  });
});
