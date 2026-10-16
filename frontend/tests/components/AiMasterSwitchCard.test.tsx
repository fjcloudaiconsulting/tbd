/**
 * TBD-627: the org AI master switch card, and the "AI is off" copy on the
 * surfaces that have a code map.
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import axe from "axe-core";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

import { apiFetch } from "@/lib/api";
import { errorText } from "@/lib/agent/chat";
import AiMasterSwitchCard from "@/components/ai/AiMasterSwitchCard";
import { fallbackReasonLabel } from "@/components/dashboard/AIForecastRefineToggle";
import { AIForecastRefinePanel } from "@/components/dashboard/AIForecastRefinePanel";

const fetchMock = vi.mocked(apiFetch);
const CONSENT = "/api/v1/settings/ai-providers/consent";
const OFF =
  "AI is off for your organization. An admin can turn it on in Settings, AI providers.";

function snapshot(over: Record<string, unknown> = {}) {
  return {
    allow_training: false,
    allow_rag: false,
    allow_telemetry: false,
    consent_version: null,
    has_consent: false,
    current_consent_version: "v2",
    ...over,
  };
}

beforeEach(() => fetchMock.mockReset());

describe("AI off copy", () => {
  it("errorText maps ai_consent_required to the one AI-off sentence", () => {
    expect(errorText("ai_consent_required")).toBe(OFF);
  });
  it("the forecast badge maps ai_consent_required to it", () => {
    expect(fallbackReasonLabel("ai_consent_required")).toBe(OFF);
  });
  it("the forecast panel renders it for ai_consent_required", async () => {
    fetchMock.mockResolvedValue({
      est_prompt_tokens: 0, est_output_tokens: 0, est_cost_cents: 0,
      duration_band: "~20-40s", can_proceed: false, reason: "ai_consent_required",
    });
    render(<AIForecastRefinePanel onApplied={() => {}} />);
    expect(await screen.findByText(OFF)).toBeInTheDocument();
  });
});

describe("AiMasterSwitchCard", () => {
  it("a stale version renders Off with the box unticked, button disabled until ticked", async () => {
    fetchMock.mockResolvedValue(snapshot({ has_consent: true, consent_version: "v1" }));
    render(<AiMasterSwitchCard />);
    expect(await screen.findByText("Off")).toBeInTheDocument();
    const box = screen.getByRole("checkbox");
    expect(box).not.toBeChecked();
    const btn = screen.getByRole("button", { name: "Turn AI on" });
    expect(btn).toBeDisabled();
    fireEvent.click(box);
    expect(btn).toBeEnabled();
  });

  it("a stale version says the terms changed; a never-accepted or current one does not", async () => {
    // FENCE (TBD-592). Wrong implementations: keying the line on !on (it then shows
    // on a fresh org that never accepted), or dropping it (a stale org reads only "Off").
    const PAUSED = /Our AI terms changed, so AI is paused\./;
    fetchMock.mockResolvedValue(snapshot({ has_consent: true, consent_version: "v1" }));
    const stale = render(<AiMasterSwitchCard />);
    expect(await stale.findByText(PAUSED)).toBeInTheDocument();
    stale.unmount();
    // A revoked consent recorded under an old version is Off by choice, not paused (mutant: `consent_version != null && !on`).
    const cases = [
      snapshot(),
      snapshot({ has_consent: true, consent_version: "v2" }),
      snapshot({ has_consent: false, consent_version: "v1" }),
    ];
    for (const s of cases) {
      fetchMock.mockResolvedValue(s);
      const view = render(<AiMasterSwitchCard />);
      await view.findByText(/AI is off until you turn it on/);
      expect(view.queryByText(PAUSED)).toBeNull();
      view.unmount();
    }
  });

  it("the consent box accepts the AI Terms and links to them", async () => {
    fetchMock.mockResolvedValue(snapshot());
    render(<AiMasterSwitchCard />);
    expect(await screen.findByRole("checkbox", { name: /^I accept the AI Terms\s*\.$/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "AI Terms" })).toHaveAttribute("href", "/ai-terms");
  });

  it("On posts the current version and NO allow_* keys", async () => {
    fetchMock.mockResolvedValueOnce(snapshot({ allow_training: false }));
    render(<AiMasterSwitchCard />);
    fireEvent.click(await screen.findByRole("checkbox"));
    fetchMock.mockResolvedValue(snapshot({ has_consent: true, consent_version: "v2" }));
    fireEvent.click(screen.getByRole("button", { name: "Turn AI on" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const [url, init] = fetchMock.mock.calls[1] as [string, { method: string; body: string }];
    expect(url).toBe(CONSENT);
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ consent_version: "v2" });
    expect(await screen.findByText("On")).toBeInTheDocument();
  });

  it("Off posts revoked: true", async () => {
    fetchMock.mockResolvedValueOnce(snapshot({ has_consent: true, consent_version: "v2" }));
    render(<AiMasterSwitchCard />);
    expect(await screen.findByText("On")).toBeInTheDocument();
    fetchMock.mockResolvedValue(snapshot());
    fireEvent.click(screen.getByRole("button", { name: "Turn AI off" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const init = fetchMock.mock.calls[1][1] as { body: string };
    expect(JSON.parse(init.body)).toEqual({ consent_version: "v2", revoked: true });
    expect(await screen.findByText("Off")).toBeInTheDocument();
  });

  it("links to the privacy section and is axe clean", async () => {
    fetchMock.mockResolvedValue(snapshot());
    const { container } = render(<AiMasterSwitchCard />);
    const link = await screen.findByRole("link", { name: "How AI uses your data" });
    expect(link).toHaveAttribute("href", "/privacy#ai-providers");
    // axe is async; next/link updates its own state meanwhile (same as assistant-chat's axe run).
    let result!: axe.AxeResults;
    await act(async () => {
      result = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
    });
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.html).join(" | ")}`)).toEqual([]);
  });
});
