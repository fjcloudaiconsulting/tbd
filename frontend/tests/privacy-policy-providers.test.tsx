// INFRA-130: the privacy policy must name the recipients it really sends data to
// and the request-log window the platform enforces (7 days, cloud-infra INFRA-130).
// INFRA-105: Hetzner hosts the service since the 2026-10-04 cutover (AWS keeps only backups), and
// Grafana Labs receives metrics and traces once cloud-infra points TBD at the collector.
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

  it("names Hetzner as the host, AWS for backups only, and Grafana Labs in section 3", () => {
    const t = text();
    const s3 = t.slice(t.indexOf("3. Third parties"), t.indexOf("4. How long"));
    expect(s3).toMatch(/Hetzner \(hosting\): runs the service/);
    expect(s3).toMatch(/Amazon Web Services \(backups\)/);
    expect(s3).not.toMatch(/Amazon Web Services \(hosting\)/);
    expect(s3).toMatch(/Grafana Labs \(Grafana Cloud, EU region/);
    expect(s3).toMatch(/never receives\s+your financial data, request contents, IP address, or request\s+logs/);
  });

  it("states where the data is stored: Hetzner Nuremberg, backups at AWS Frankfurt", () => {
    const t = text();
    const s7 = t.slice(t.indexOf("7. International transfers"), t.indexOf("8. Children"));
    expect(s7).toMatch(/runs at Hetzner in\s+Nuremberg/);
    expect(s7).not.toMatch(/Your data is stored in the EU \(Amazon Web Services/);
  });

  it("states request logs are kept up to 7 days, never 30", () => {
    const t = text();
    expect(t.match(/up to 7 days/g)?.length).toBe(2);
    expect(t).not.toMatch(/up to 30 days/);
  });

  it("states server backups containing request logs are deleted within 14 days", () => {
    expect(text()).toContain("Backups of our servers that may contain these logs are deleted within 14 days.");
  });

  it("does not promise nothing is sent when no AI provider is configured", () => {
    expect(text()).not.toMatch(/do not send it anywhere/);
  });
});
