import React, { useState } from "react";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

import ModelPicker from "@/components/ai/ModelPicker";
import { apiFetch } from "@/lib/api";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

const M = (id: string, extra: Record<string, unknown> = {}) => ({
  id,
  label: null,
  hint: null,
  tier: null,
  prompt_micros_per_mtok: null,
  completion_micros_per_mtok: null,
  replies_left: null,
  ...extra,
});

// Deliberately NOT alphabetical and not by tier: server order is the contract.
const LIST = {
  default: "zeta-mini",
  replies_period: "month" as const,
  models: [
    M("zeta-mini", { label: "Zeta Mini", tier: "budget", hint: "Fast and cheap", replies_left: 1200 }),
    M("ap-7", { label: "Alpha Pro", tier: "frontier", hint: "Best reasoning" }),
    M("mid-model", { tier: "standard" }),
  ],
};

function serve(byUrl: Record<string, unknown>) {
  vi.mocked(apiFetch).mockImplementation(async (url: string) => {
    for (const [k, v] of Object.entries(byUrl)) {
      if (url.includes(k)) {
        if (v instanceof Error) throw v;
        return v as never;
      }
    }
    return undefined as never;
  });
}

function Harness({
  cred = 42,
  initial = "",
  feature,
  platform,
  onListed,
}: {
  cred?: number | "";
  initial?: string;
  feature?: string;
  platform?: boolean;
  onListed?: (b: boolean) => void;
}) {
  const [v, setV] = useState(initial);
  const [c, setC] = useState<number | "">(cred);
  return (
    <div>
      <button type="button" onClick={() => setC(7)}>switch-cred</button>
      <span data-testid="draft">{v}</span>
      <ModelPicker
        id="mp"
        credentialId={c}
        feature={feature}
        platform={platform}
        value={v}
        onChange={setV}
        onListedChange={onListed}
      />
    </div>
  );
}

const calls = () => vi.mocked(apiFetch).mock.calls.map((c) => [c[0], (c[1] as RequestInit | undefined)?.method ?? "GET"]);
const combo = () => screen.getByRole("combobox");
const optionTexts = () => screen.getAllByRole("option").map((o) => o.textContent ?? "");

describe("ModelPicker", () => {
  beforeEach(() => { vi.mocked(apiFetch).mockReset(); });

  it("P1 renders options in server order with label, tier word and hint", async () => {
    serve({ "/42/models": LIST });
    render(<Harness />);
    await waitFor(() => expect(combo()).not.toBeDisabled());
    fireEvent.focus(combo());
    const opts = optionTexts();
    expect(opts).toHaveLength(3);
    expect(opts[0]).toContain("Zeta Mini");
    expect(opts[1]).toContain("Alpha Pro");
    expect(opts[2]).toContain("mid-model");
    expect(opts[0]).toContain("Budget");
    expect(opts[0]).toContain("Fast and cheap");
    expect(opts[1]).toContain("Frontier");
    expect(opts[2]).toContain("Standard");
    expect(opts[2]).not.toContain("Budget");
  });

  it("P2 filters by label and by id, keeping server order", async () => {
    serve({ "/42/models": LIST });
    render(<Harness />);
    await waitFor(() => expect(combo()).not.toBeDisabled());
    fireEvent.focus(combo());
    fireEvent.change(combo(), { target: { value: "PRO" } });
    expect(optionTexts()).toHaveLength(1);
    expect(optionTexts()[0]).toContain("Alpha Pro");
    fireEvent.change(combo(), { target: { value: "mid-m" } });
    expect(optionTexts()).toHaveLength(1);
    expect(optionTexts()[0]).toContain("mid-model");
    // the id of a labelled model matches too, and order is the server's
    fireEvent.change(combo(), { target: { value: "a" } });
    const ids = optionTexts();
    expect(ids[0]).toContain("Zeta Mini");
    expect(ids[1]).toContain("Alpha Pro");
  });

  it("P3 shows replies left with the period, only when present", async () => {
    serve({ "/42/models": LIST });
    const { unmount } = render(<Harness />);
    await waitFor(() => expect(combo()).not.toBeDisabled());
    fireEvent.focus(combo());
    expect(optionTexts()[0]).toContain("about 1,200 replies left this month");
    expect(optionTexts()[1]).not.toMatch(/replies left/);
    unmount();
    serve({ "/42/models": { ...LIST, replies_period: "day" } });
    render(<Harness />);
    await waitFor(() => expect(combo()).not.toBeDisabled());
    fireEvent.focus(combo());
    expect(optionTexts()[0]).toContain("about 1,200 replies left today");
  });

  it("P4 preselects the default for an empty draft and re-preselects on credential switch", async () => {
    serve({
      "/42/models": LIST,
      "/7/models": { default: "other-model", replies_period: null, models: [M("other-model"), M("zeta-mini")] },
    });
    render(<Harness />);
    await waitFor(() => expect(screen.getByTestId("draft")).toHaveTextContent("zeta-mini"));
    fireEvent.click(screen.getByText("switch-cred"));
    await waitFor(() => expect(screen.getByTestId("draft")).toHaveTextContent("other-model"));
  });

  it("P4b keeps a saved listed model on mount", async () => {
    serve({ "/42/models": LIST });
    render(<Harness initial="ap-7" />);
    await waitFor(() => expect(combo()).not.toBeDisabled());
    expect(screen.getByTestId("draft")).toHaveTextContent("ap-7");
  });

  it("P5 stale saved model stays, warns, reports unlisted, and never rewrites", async () => {
    serve({ "/42/models": LIST });
    const listed = vi.fn();
    render(<Harness initial="gpt-4o" onListed={listed} />);
    await waitFor(() => expect(combo()).not.toBeDisabled());
    expect(combo()).toHaveValue("gpt-4o");
    expect(screen.getByText(/No longer in this key's list\. Pick another to change it\./)).toBeInTheDocument();
    expect(screen.getByTestId("draft")).toHaveTextContent("gpt-4o");
    expect(listed).toHaveBeenLastCalledWith(false);
    fireEvent.focus(combo());
    fireEvent.click(screen.getAllByRole("option")[1]);
    expect(screen.getByTestId("draft")).toHaveTextContent("ap-7");
    expect(listed).toHaveBeenLastCalledWith(true);
    expect(screen.queryByText(/No longer in this key's list/)).not.toBeInTheDocument();
  });

  it("P6 typed text that matches nothing never becomes the value", async () => {
    serve({ "/42/models": LIST });
    render(<Harness initial="ap-7" />);
    await waitFor(() => expect(combo()).not.toBeDisabled());
    fireEvent.focus(combo());
    fireEvent.change(combo(), { target: { value: "totally-made-up" } });
    fireEvent.keyDown(combo(), { key: "Enter" });
    expect(screen.queryAllByRole("option")).toHaveLength(0);
    expect(screen.getByTestId("draft")).toHaveTextContent("ap-7");
    fireEvent.blur(combo());
    fireEvent.mouseDown(document.body);
    expect(screen.getByTestId("draft")).toHaveTextContent("ap-7");
  });

  it("P7 default row GETs /models without feature, feature row passes it", async () => {
    serve({ "/42/models": LIST });
    const { unmount } = render(<Harness />);
    await waitFor(() => expect(combo()).not.toBeDisabled());
    expect(calls()).toContainEqual(["/api/v1/settings/ai-providers/42/models", "GET"]);
    unmount();
    vi.mocked(apiFetch).mockClear();
    render(<Harness feature="smart_plan" />);
    await waitFor(() => expect(combo()).not.toBeDisabled());
    expect(calls()).toContainEqual(["/api/v1/settings/ai-providers/42/models?feature=smart_plan", "GET"]);
  });

  it("P8 refresh POSTs validate then re-GETs the list; hidden for platform", async () => {
    serve({ "/42/models": LIST, "/42/validate": {} });
    const { unmount } = render(<Harness />);
    await waitFor(() => expect(combo()).not.toBeDisabled());
    vi.mocked(apiFetch).mockClear();
    serve({ "/42/models": LIST, "/42/validate": {} });
    fireEvent.click(screen.getByRole("button", { name: "Refresh models" }));
    await waitFor(() =>
      expect(calls()).toEqual([
        ["/api/v1/settings/ai-providers/42/validate", "POST"],
        ["/api/v1/settings/ai-providers/42/models", "GET"],
      ]),
    );
    unmount();
    render(<Harness platform />);
    await waitFor(() => expect(combo()).not.toBeDisabled());
    expect(screen.queryByRole("button", { name: "Refresh models" })).not.toBeInTheDocument();
  });

  it("P9 empty list shows the empty copy; fetch error offers Retry", async () => {
    serve({ "/42/models": { default: null, replies_period: null, models: [] } });
    const { unmount } = render(<Harness />);
    expect(
      await screen.findByText("This key has no model that can do this. Refresh models or pick another key."),
    ).toBeInTheDocument();
    unmount();
    let fail = true;
    vi.mocked(apiFetch).mockImplementation(async () => {
      if (fail) throw new Error("boom");
      return LIST as never;
    });
    render(<Harness />);
    const retry = await screen.findByRole("button", { name: "Retry" });
    fail = false;
    fireEvent.click(retry);
    await waitFor(() => expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument());
    expect(screen.getByTestId("draft")).toHaveTextContent("zeta-mini");
  });

  it("shows a disabled Loading models field while the list is in flight", async () => {
    let release: (v: unknown) => void = () => {};
    vi.mocked(apiFetch).mockImplementation(() => new Promise((r) => { release = r; }) as never);
    render(<Harness />);
    expect(combo()).toBeDisabled();
    expect(combo()).toHaveAttribute("placeholder", "Loading models…");
    release(LIST);
    await waitFor(() => expect(combo()).not.toBeDisabled());
  });

  it("P11 keyboard: ArrowDown + Enter selects, Escape closes without change", async () => {
    serve({ "/42/models": LIST });
    render(<Harness initial="mid-model" />);
    await waitFor(() => expect(combo()).not.toBeDisabled());
    fireEvent.focus(combo());
    fireEvent.keyDown(combo(), { key: "ArrowDown" });
    fireEvent.keyDown(combo(), { key: "ArrowDown" });
    fireEvent.keyDown(combo(), { key: "Enter" });
    expect(screen.getByTestId("draft")).toHaveTextContent("ap-7");
    expect(screen.queryAllByRole("option")).toHaveLength(0);
    fireEvent.focus(combo());
    fireEvent.keyDown(combo(), { key: "ArrowDown" });
    fireEvent.keyDown(combo(), { key: "Escape" });
    expect(screen.queryAllByRole("option")).toHaveLength(0);
    expect(screen.getByTestId("draft")).toHaveTextContent("ap-7");
  });
});
