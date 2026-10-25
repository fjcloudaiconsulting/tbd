// TBD-592: /ai-terms publishes the version an org consents to. The backend twin
// (backend/tests/test_ai_terms_version_frontend_contract.py) pins the same fixture
// to the enforced default, so the published and enforced versions cannot drift.
import React from "react";
import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import AiTermsPage from "@/app/ai-terms/page";
import { AI_TERMS_VERSION } from "@/lib/aiTerms";
import fixture from "./fixtures/ai-terms-version.json";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/ai-terms",
}));

describe("/ai-terms", () => {
  it("FENCE: the constant equals the shared fixture", () => {
    expect(AI_TERMS_VERSION).toBe(fixture.version);
  });

  it("renders the version and no em dash", () => {
    const t = render(<AiTermsPage />).container.textContent ?? "";
    expect(t).toContain(`Version ${fixture.version}`);
    expect(t).not.toMatch(/—/);
  });

  it("names the own-key providers and no longer offers Ollama (TBD-584)", () => {
    // An editorial edit that narrows processing keeps the version (the fence above).
    const t = render(<AiTermsPage />).container.textContent ?? "";
    expect(t).toMatch(/OpenAI, Anthropic, or an OpenAI-compatible\s+service such as OpenRouter\)/);
    expect(t).not.toMatch(/ollama/i);
  });

  it("claims no DPA or SCCs, and states the AI exception to EU storage", () => {
    const t = render(<AiTermsPage />).container.textContent ?? "";
    expect(t).not.toMatch(/data processing agreement|Standard Contractual Clauses|\bDPA\b|\bSCCs?\b/);
    expect(t).toMatch(/may process it outside the EU, including in the\s+United States/);
  });
});
