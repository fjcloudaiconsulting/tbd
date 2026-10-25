// INFRA-130: the privacy policy must name the recipients it really sends data to
// and the request-log window the platform enforces (7 days, cloud-infra INFRA-130).
// INFRA-105: Hetzner hosts the service since the 2026-10-04 cutover (AWS keeps only backups), and
// Grafana Labs receives metrics and traces once cloud-infra points TBD at the collector.
import React from "react";
import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import PrivacyPolicyPage from "@/app/privacy/page";
import TermsPage from "@/app/terms/page";
import { faqEntries } from "@/components/landing/faqData";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/privacy",
}));

const text = () => render(<PrivacyPolicyPage />).container.textContent ?? "";

describe("privacy policy recipients and retention", () => {
  it("lists Google Analytics and the bring-your-own-key AI provider in section 3", () => {
    const t = text();
    const s3 = t.slice(t.indexOf("3. Third parties"), t.indexOf("4. AI features"));
    expect(s3).toContain("Google (Analytics");
    expect(s3).toMatch(/AI providers \(only while AI is on for your\s+organization\): the provider your organization sets up with its\s+own key/);
  });

  it("names Hetzner as the host, AWS for backups only, and Grafana Labs in section 3", () => {
    const t = text();
    const s3 = t.slice(t.indexOf("3. Third parties"), t.indexOf("4. AI features"));
    expect(s3).toMatch(/Hetzner \(hosting\): runs the service/);
    expect(s3).toMatch(/Amazon Web Services \(backups\)/);
    expect(s3).not.toMatch(/Amazon Web Services \(hosting\)/);
    expect(s3).toMatch(/Grafana Labs \(Grafana Cloud, EU region/);
    expect(s3).toMatch(/never receives\s+your financial data, request contents, IP address, or request\s+logs/);
  });

  it("states where the data is stored: Hetzner Nuremberg, backups at AWS Frankfurt", () => {
    const t = text();
    const s7 = t.slice(t.indexOf("8. International transfers"), t.indexOf("9. Children"));
    expect(s7).toMatch(/runs at Hetzner in\s+Nuremberg/);
    expect(s7).toMatch(/backups are kept at Amazon Web\s+Services/);
    expect(s7).toMatch(/Grafana Cloud stores\s+operational metrics and traces in its EU region/);
  });

  it("states Grafana Cloud retention in section 4", () => {
    const t = text();
    const s4 = t.slice(t.indexOf("5. How long"), t.indexOf("6. Your rights"));
    expect(s4).toMatch(/Metrics and traces at Grafana Cloud follow its retention/);
  });

  it("terms name Hetzner for hosting and AWS for backups only", () => {
    const t = render(<TermsPage />).container.textContent ?? "";
    expect(t).toMatch(/Hetzner for hosting,\s+Amazon Web Services for backups, Grafana Labs for monitoring/);
    expect(t).not.toMatch(/Amazon Web Services for\s+hosting/);
  });

  it("the FAQ does not claim the database is encrypted at rest (local disks on Hetzner are not)", () => {
    const security = faqEntries.find((e) => e.q === "Is my data secure?");
    expect(security?.a).toMatch(/stays in the EU: the service and its database run in Germany/);
    expect(security?.a).not.toMatch(/encrypted at rest/);
  });

  it("states request logs are kept up to 7 days, never 30", () => {
    const t = text();
    expect(t.match(/up to 7 days/g)?.length).toBe(2);
    expect(t).not.toMatch(/up to 30 days/);
  });

  it("states server backups containing request logs are deleted within 14 days", () => {
    expect(text()).toContain("Backups of our servers that may contain these logs are deleted within 14 days.");
  });

  it("section 4 says AI data may leave the EU under the provider's terms, and claims no DPA", () => {
    // FENCE (TBD-592). Wrong implementations: claiming a signed DPA or the SCCs (the
    // operator cannot confirm either), or an EU-only promise with no AI exception.
    const t = text();
    const s4 = t.slice(t.indexOf("4. AI features"), t.indexOf("5. How long"));
    expect(s4).toMatch(/stays in the EU \(section 8\)/);
    expect(s4).toMatch(/may process it outside the EU, including in the\s+United States, under that provider\u2019s own terms/);
    const s8 = t.slice(t.indexOf("8. International transfers"), t.indexOf("9. Children"));
    expect(s8).toMatch(/AI features are the exception/);
    expect(t).not.toMatch(/data processing agreement|Standard Contractual Clauses|\bDPA\b|\bSCCs?\b/);
  });

  it("states the 48-hour proposal window in sections 4 and 5, with no lifetime claim", () => {
    // FENCE (TBD-592): the copy must match the purge job (48 hours), never the old
    // "kept for as long as your organization's data exists".
    const t = text();
    const s4 = t.slice(t.indexOf("4. AI features"), t.indexOf("5. How long"));
    const s5 = t.slice(t.indexOf("5. How long"), t.indexOf("6. Your rights"));
    expect(s4).toMatch(/48 hours\s+after the proposal was made/);
    expect(s5).toMatch(/deleted 48 hours after the proposal was made/);
    expect(t).not.toMatch(/as long as your organization\u2019s data exists/);
  });

  it("the AI section carries the anchor the AI card links to", () => {
    const { container } = render(<PrivacyPolicyPage />);
    expect(container.querySelector("#ai-providers h2")?.textContent).toBe("4. AI features");
  });

  it("section 4 names the own-key providers and no longer offers Ollama (TBD-584)", () => {
    const t = text();
    const s4 = t.slice(t.indexOf("4. AI features"), t.indexOf("5. How long"));
    expect(s4).toMatch(/OpenAI, Anthropic, or an OpenAI-compatible service\s+such as OpenRouter/);
    expect(t).not.toMatch(/ollama/i);
  });

  it("does not promise nothing is sent when no AI provider is configured", () => {
    expect(text()).not.toMatch(/do not send it anywhere/);
  });
});
