/**
 * TBD-581 agent access page: F-581-AUTO, F-581-DOWN, F-581-DRIFT,
 * F-581-REVERT-ERR, F-581-SSO, and an axe pass (F-U4, WCAG 2.2 AA).
 * Real apiFetch over a routed global fetch.
 */
import React from "react";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import axe from "axe-core";

import AgentTokensPage from "@/app/settings/agent-tokens/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { setAccessToken } from "@/lib/api";
import { useAiStatus } from "@/lib/hooks/use-ai-status";
import type { AgentActionRow, AgentToken } from "@/lib/agent/types";
import type { AIStatus, User } from "@/lib/types";
import { renderWithSWR } from "@/tests/utils/render-with-swr";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/settings/agent-tokens",
}));
vi.mock("@/components/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));
vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<typeof import("@/components/auth/AuthProvider")>(
    "@/components/auth/AuthProvider",
  );
  return { ...actual, useAuth: vi.fn() };
});
vi.mock("@/lib/hooks/use-ai-status", () => ({ useAiStatus: vi.fn() }));

const USER = {
  id: 1, username: "demo", email: "d@x.io", role: "member", org_id: 1, is_superadmin: false,
  is_active: true, mfa_enabled: false, password_set: true,
} as unknown as User;

const STATUS: AIStatus = {
  categorize: { entitled: true, configured: true },
  forecast: { entitled: true, configured: true },
  budget: { entitled: true, configured: true },
  agent: { entitled: true, configured: true },
  usage: {
    "assistant.turns": { used: 4, limit: 50, period: "day", resets_at: "2026-10-10T00:00:00+00:00" },
    "mcp.calls": { used: 10, limit: null, period: "month", resets_at: "2026-11-01T00:00:00+00:00" },
  },
};

const token = (over: Partial<AgentToken>): AgentToken => ({
  id: 5, name: "Claude Desktop", prefix: "pat_ab12", scope: "agent:auto",
  created_at: "2026-10-01T10:00:00", expires_at: "2099-10-31T10:00:00",
  last_used_at: "2026-10-08T09:00:00", last_used_ip: "203.0.113.4", status: "active", ...over,
});

const DONE: AgentActionRow = {
  action_id: "x1", tool: "budgets_update_amount", channel: "mcp", risk: "write", mode: "auto",
  status: "done",
  preview: {
    summary: "Change the amount of budget 12 from 400.00 to 450.00 EUR",
    changes: [{ entity: "budgets", id: 12, field: "amount", before: "400.00", after: "450.00", currency: "EUR" }],
    warnings: [], context: { category_name: { untrusted: "Groceries" } },
  },
  error_code: null, created_at: "2026-10-08T09:00:00", decided_at: "2026-10-08T09:00:01",
};

type Route = (init: RequestInit) => Response;
let routes: Record<string, Route>;
let fetchMock: ReturnType<typeof vi.fn>;
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

beforeEach(() => {
  setAccessToken("t");
  vi.mocked(useAuth).mockReturnValue({ user: USER, loading: false } as unknown as ReturnType<typeof useAuth>);
  vi.mocked(useAiStatus).mockReturnValue(STATUS);
  routes = {
    "GET /api/v1/agent/tokens": () => json(200, { items: [token({})], total: 1, limit: 1, offset: 0 }),
    "GET /api/v1/agent/actions?status=done&limit=20": () => json(200, { items: [DONE], limit: 20, offset: 0 }),
  };
  fetchMock = vi.fn(async (url: string, init: RequestInit = {}) => {
    const key = `${init.method ?? "GET"} ${String(url)}`;
    const r = routes[key];
    if (!r) throw new Error(`unrouted ${key}`);
    return r(init);
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  setAccessToken(null);
  sessionStorage.clear();
});

const body = (key: string) => {
  const call = fetchMock.mock.calls.find(([u, i]) => `${(i as RequestInit)?.method ?? "GET"} ${u}` === key);
  return call ? JSON.parse((call[1] as RequestInit).body as string) : undefined;
};

describe("mint", () => {
  it("F-581-AUTO: auto-apply needs the acknowledgment and lasts at most 30 days", async () => {
    routes["POST /api/v1/agent/tokens"] = () =>
      json(201, { token: "pat_secret", id: 9, name: "bot", prefix: "pat_cd", scope: "agent:auto",
        created_at: "2026-10-09T10:00:00", expires_at: "2026-11-08T10:00:00" });
    renderWithSWR(<AgentTokensPage />);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "bot" } });
    fireEvent.change(screen.getByLabelText("Expires in"), { target: { value: "90" } });
    fireEvent.click(screen.getByRole("radio", { name: /Auto-apply/ }));

    const expiry = screen.getByLabelText("Expires in") as HTMLSelectElement;
    expect(Array.from(expiry.options).map((o) => o.value)).toEqual(["7", "30"]);
    expect(expiry.value).toBe("30");

    const create = screen.getByRole("button", { name: "Create token" });
    expect(create).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(create);
    expect(screen.queryByTestId("stepup-modal")).toBeNull();

    fireEvent.click(screen.getByRole("checkbox", { name: /changes apply without asking me/ }));
    fireEvent.click(create);
    fireEvent.change(screen.getByTestId("stepup-password"), { target: { value: "pw" } });
    fireEvent.click(screen.getByTestId("stepup-submit"));
    await waitFor(() => expect(screen.getByTestId("reveal-panel")).toBeInTheDocument());
    expect(body("POST /api/v1/agent/tokens")).toEqual({
      name: "bot", scope: "agent:auto", expires_in_days: 30, acknowledge_auto: true, current_password: "pw",
    });
    // The reveal panel names the access, never "Read-only".
    expect(within(screen.getByTestId("reveal-panel")).getByText("Auto-apply")).toBeInTheDocument();
  });

  it("F-581-SSO: a password-less member verifies with Google for agent_token_mint, and the proof comes back", async () => {
    vi.mocked(useAuth).mockReturnValue({ user: { ...USER, password_set: false }, loading: false } as unknown as ReturnType<typeof useAuth>);
    routes["POST /api/v1/auth/sso-stepup/initiate"] = () => json(200, { redirect_url: "about:blank#google" });
    const { unmount } = renderWithSWR(<AgentTokensPage />);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "bot" } });
    fireEvent.click(screen.getByRole("button", { name: "Create token" }));
    fireEvent.click(screen.getByTestId("stepup-sso-verify"));
    await waitFor(() => expect(body("POST /api/v1/auth/sso-stepup/initiate")).toEqual({ action: "agent_token_mint" }));
    unmount();

    // Back from Google with the proof in the fragment.
    window.history.replaceState(null, "", "/settings/agent-tokens#stepup_token=proof123");
    routes["POST /api/v1/agent/tokens"] = () =>
      json(201, { token: "pat_s", id: 9, name: "bot", prefix: "pat_cd", scope: "agent:read",
        created_at: "2026-10-09T10:00:00", expires_at: "2026-11-08T10:00:00" });
    renderWithSWR(<AgentTokensPage />);
    expect(window.location.hash).toBe("");
    fireEvent.click(await screen.findByTestId("stepup-submit"));
    await waitFor(() => expect(body("POST /api/v1/agent/tokens")).toMatchObject({
      name: "bot", scope: "agent:read", stepup_token: "proof123",
    }));
  });

  it("hides minting when the plan has no agent calls, but keeps revoke", async () => {
    vi.mocked(useAiStatus).mockReturnValue({ ...STATUS, usage: { ...STATUS.usage, "mcp.calls": { used: 0, limit: 0, period: "month", resets_at: null } } });
    renderWithSWR(<AgentTokensPage />);
    expect(screen.queryByTestId("agent-mint-form")).toBeNull();
    expect(await screen.findByRole("button", { name: "Revoke Claude Desktop" })).toBeInTheDocument();
  });
});

describe("tokens", () => {
  it("F-581-DOWN: lowering offers only strictly lower access and sends it", async () => {
    routes["PATCH /api/v1/agent/tokens/5"] = () => json(200, token({ scope: "agent:read" }));
    renderWithSWR(<AgentTokensPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Lower access for Claude Desktop" }));
    const dialog = screen.getByRole("dialog");
    const options = within(dialog).getAllByRole("radio").map((r) => (r as HTMLInputElement).value);
    expect(options).toEqual(["agent:read", "agent:write"]);
    fireEvent.click(within(dialog).getByRole("radio", { name: /^Read Reads/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Lower access" }));
    await waitFor(() => expect(body("PATCH /api/v1/agent/tokens/5")).toEqual({ scope: "agent:read" }));
  });

  it("F-581-DOWN: a write token can only drop to read, never rise to auto", async () => {
    routes["GET /api/v1/agent/tokens"] = () => json(200, { items: [token({ scope: "agent:write" })], total: 1, limit: 1, offset: 0 });
    renderWithSWR(<AgentTokensPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Lower access for Claude Desktop" }));
    const options = within(screen.getByRole("dialog")).getAllByRole("radio").map((r) => (r as HTMLInputElement).value);
    expect(options).toEqual(["agent:read"]);
  });

  it("a read token offers no lowering", async () => {
    routes["GET /api/v1/agent/tokens"] = () => json(200, { items: [token({ scope: "agent:read" })], total: 1, limit: 1, offset: 0 });
    renderWithSWR(<AgentTokensPage />);
    await screen.findByRole("button", { name: "Revoke Claude Desktop" });
    expect(screen.queryByRole("button", { name: /Lower access/ })).toBeNull();
  });
});

describe("activity", () => {
  it("marks auto-applied rows", async () => {
    renderWithSWR(<AgentTokensPage />);
    const row = (await screen.findByText("Change a budget amount")).closest("li")!;
    expect(within(row).getByText("Auto")).toBeInTheDocument();
    expect(row.textContent).toMatch(/€400\.00/);
  });

  it("F-581-DRIFT: drift shows before the revert applies, and applying posts the staged id", async () => {
    routes["POST /api/v1/agent/actions/x1/revert"] = () => json(409, { detail: {
      code: "revert_drift", message: "the data changed since the action ran",
      action_id: "r9", summary: "Change the amount of budget 12 from 470.00 to 400.00 EUR",
      changes: [{ entity: "budgets", id: 12, field: "amount", before: "470.00", after: "400.00", currency: "EUR" }],
      warnings: [], context: { category_name: { untrusted: "Groceries" }, reverts: "x1" },
      expires_at: "2026-10-09T12:10:00", requires_confirmation: true,
      drift: [{ entity: "budgets", id: 12, field: "amount", expected: "450.00", current: "470.00" }],
    } });
    routes["POST /api/v1/agent/actions/r9/confirm"] = () => json(200, { action_id: "r9", status: "done" });
    renderWithSWR(<AgentTokensPage />);
    fireEvent.click(await screen.findByRole("button", { name: /^Revert: / }));
    const dialog = await screen.findByRole("dialog");
    const drift = within(dialog).getByTestId("drift");
    expect(within(drift).getByRole("cell", { name: "Budget amount" })).toBeInTheDocument();
    expect(drift.textContent).toMatch(/€450\.00/);
    expect(drift.textContent).toMatch(/€470\.00/);
    fireEvent.click(within(dialog).getByRole("button", { name: "Revert anyway" }));
    await waitFor(() => expect(within(dialog).getByText("Applied")).toBeInTheDocument());
    expect(fetchMock.mock.calls.some(([u]) => u === "/api/v1/agent/actions/r9/confirm")).toBe(true);
    expect(fetchMock.mock.calls.some(([u]) => u === "/api/v1/agent/actions/x1/confirm")).toBe(false);
  });

  it.each([
    [409, { code: "not_revertible", message: "m", reason: "no_inverse" }, /has no undo/],
    [409, { code: "not_revertible", message: "m", reason: "not_done" }, /Only an applied change/],
    [410, { code: "tool_retired", message: "m" }, /no longer available/],
    [422, { code: "no_change", message: "m" }, /Nothing to revert/],
  ])("F-581-REVERT-ERR: %s %o", async (status, detail, text) => {
    routes["POST /api/v1/agent/actions/x1/revert"] = () => json(status, { detail });
    renderWithSWR(<AgentTokensPage />);
    fireEvent.click(await screen.findByRole("button", { name: /^Revert: / }));
    expect(await screen.findByText(text)).toHaveAttribute("role", "status");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("F-581-REVERT-ERR: a read action offers no revert", async () => {
    routes["GET /api/v1/agent/actions?status=done&limit=20"] = () =>
      json(200, { items: [{ ...DONE, action_id: "r0", risk: "read", mode: "confirm" }], limit: 20, offset: 0 });
    renderWithSWR(<AgentTokensPage />);
    await screen.findByText("Change a budget amount");
    expect(screen.queryByRole("button", { name: /^Revert: / })).toBeNull();
  });

  it("closing a staged revert without deciding discards it", async () => {
    routes["POST /api/v1/agent/actions/x1/revert"] = () => json(200, {
      action_id: "r5", summary: "", changes: DONE.preview.changes, warnings: [], context: {},
      expires_at: "2026-10-09T12:10:00", requires_confirmation: true,
    });
    routes["POST /api/v1/agent/actions/r5/cancel"] = () => json(200, { action_id: "r5", status: "cancelled" });
    renderWithSWR(<AgentTokensPage />);
    fireEvent.click(await screen.findByRole("button", { name: /^Revert: / }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([u]) => u === "/api/v1/agent/actions/r5/cancel")).toBe(true));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("a plan without the agent hides activity but keeps the tokens", async () => {
    routes["GET /api/v1/agent/actions?status=done&limit=20"] = () =>
      json(403, { detail: { code: "feature_not_enabled", feature_key: "ai.agent" } });
    renderWithSWR(<AgentTokensPage />);
    expect(await screen.findByRole("button", { name: "Revoke Claude Desktop" })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("Agent activity")).toBeNull());
  });

  it("F-U4: the page is axe clean", async () => {
    const { container } = renderWithSWR(<AgentTokensPage />);
    await screen.findByText("Change a budget amount");
    const result = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.html).join(" | ")}`)).toEqual([]);
  });
});
