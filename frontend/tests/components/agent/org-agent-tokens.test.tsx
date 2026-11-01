/**
 * TBD-619 org admin view of members' agent tokens: admin sees rows, a member
 * never fetches the org route, Revoke hits the ORG delete route, a 403 renders
 * nothing broken, axe clean.
 */
import React from "react";
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import axe from "axe-core";

import AgentTokensPage from "@/app/settings/agent-tokens/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { setAccessToken } from "@/lib/api";
import { useAiStatus } from "@/lib/hooks/use-ai-status";
import type { AgentToken, OrgAgentToken } from "@/lib/agent/types";
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

const user = (role: string) =>
  ({ id: 1, username: "demo", email: "d@x.io", role, org_id: 1, is_superadmin: false, is_active: true,
    mfa_enabled: false, password_set: true }) as unknown as User;

const STATUS: AIStatus = {
  categorize: { entitled: true, configured: true },
  forecast: { entitled: true, configured: true },
  budget: { entitled: true, configured: true },
  agent: { entitled: true, configured: true },
  usage: {
    "assistant.turns": { used: 0, limit: 50, period: "day", resets_at: "2026-10-10T00:00:00+00:00" },
    "mcp.calls": { used: 0, limit: null, period: "month", resets_at: "2026-11-01T00:00:00+00:00" },
  },
};

const orgToken = (over: Partial<OrgAgentToken>): OrgAgentToken => ({
  id: 77, name: "Ana bot", prefix: "pat_zz99", scope: "agent:read",
  created_at: "2026-10-01T10:00:00", expires_at: "2099-10-31T10:00:00",
  last_used_at: null, last_used_ip: null, status: "active",
  owner_user_id: 9, owner_email: "ana@x.io", ...over,
});

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const list = (items: unknown[]) => json(200, { items, total: items.length, limit: items.length, offset: 0 });

let orgItems: OrgAgentToken[];
// The admin's own token (id 5) is in both lists; one flag drives both.
let ownRevoked: boolean;
let ownEmpty = false;
const own = (): AgentToken => ({ ...orgToken({ id: 5, name: "My bot", owner_email: "d@x.io", owner_user_id: 1,
  scope: "agent:auto", status: ownRevoked ? "revoked" : "active" }) });
const ownRow = () => {
  const { owner_user_id: _a, owner_email: _b, ...t } = own() as OrgAgentToken;
  return t;
};
let orgStatus: number;
let fetchMock: ReturnType<typeof vi.fn>;
const calls = () => fetchMock.mock.calls.map(([u, i]) => `${(i as RequestInit)?.method ?? "GET"} ${u}`);

beforeEach(() => {
  setAccessToken("t");
  orgItems = [orgToken({})];
  orgStatus = 200;
  ownRevoked = false;
  ownEmpty = false;
  vi.mocked(useAuth).mockReturnValue({ user: user("admin"), loading: false } as unknown as ReturnType<typeof useAuth>);
  vi.mocked(useAiStatus).mockReturnValue(STATUS);
  fetchMock = vi.fn(async (url: string, init: RequestInit = {}) => {
    const key = `${init.method ?? "GET"} ${String(url)}`;
    if (key === "GET /api/v1/agent/tokens") return list(!ownEmpty && (ownRevoked || orgItems.length) ? [ownRow()] : []);
    if (key === "DELETE /api/v1/agent/tokens/5" || key === "DELETE /api/v1/agent/tokens/org/5") {
      ownRevoked = true;
      return json(200, { ok: true, id: 5 });
    }
    if (key === "GET /api/v1/agent/actions?status=done&limit=20") return list([]);
    if (key === "GET /api/v1/agent/actions?status=pending&limit=20") return list([]);
    if (key === "GET /api/v1/agent/tokens/org") {
      if (orgStatus !== 200) return json(orgStatus, { detail: orgStatus === 403 ? "Forbidden" : "Boom" });
      return list(orgItems.length ? [...orgItems, { ...own(), owner_user_id: 1, owner_email: "d@x.io" }] : []);
    }
    if (key === "DELETE /api/v1/agent/tokens/org/77") {
      orgItems = [orgToken({ status: "revoked" })];
      return json(200, { ok: true, id: 77 });
    }
    throw new Error(`unrouted ${key}`);
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  setAccessToken(null);
});

describe("org agent tokens", () => {
  it("F-619-ADMIN: an admin sees the org section with owner and token rows", async () => {
    renderWithSWR(<AgentTokensPage />);
    const section = await screen.findByRole("region", { name: "Organization agent tokens" });
    const row = await within(section).findByTestId("agent-token-77");
    expect(within(row).getByText("ana@x.io")).toBeInTheDocument();
    expect(within(row).getByText("Ana bot")).toBeInTheDocument();
    expect(within(section).getByRole("columnheader", { name: "Owner" })).toBeInTheDocument();
  });

  it("F-619-MEMBER: a member never renders the section and never calls the org route", async () => {
    vi.mocked(useAuth).mockReturnValue({ user: user("member"), loading: false } as unknown as ReturnType<typeof useAuth>);
    renderWithSWR(<AgentTokensPage />);
    await screen.findByText("Your agent tokens");
    await waitFor(() => expect(calls()).toContain("GET /api/v1/agent/tokens"));
    expect(screen.queryByText("Organization agent tokens")).toBeNull();
    expect(calls().some((c) => c.includes("/agent/tokens/org"))).toBe(false);
  });

  it("F-619-REVOKE: Revoke asks first, then DELETEs the org route with the id and the row updates", async () => {
    renderWithSWR(<AgentTokensPage />);
    const section = await screen.findByRole("region", { name: "Organization agent tokens" });
    fireEvent.click(await within(section).findByRole("button", { name: "Revoke Ana bot owned by ana@x.io" }));
    expect(calls().some((c) => c.startsWith("DELETE"))).toBe(false);
    fireEvent.click(await screen.findByRole("button", { name: "Revoke token" }));
    await waitFor(() => expect(calls()).toContain("DELETE /api/v1/agent/tokens/org/77"));
    expect(calls()).not.toContain("DELETE /api/v1/agent/tokens/77");
    await within(section).findByText("Revoked");
    expect(within(section).queryByRole("button", { name: /^Revoke Ana bot/ })).toBeNull();
  });

  it("F-619-403: a 403 from the org list renders no section and no error", async () => {
    orgStatus = 403;
    renderWithSWR(<AgentTokensPage />);
    await waitFor(() => expect(calls()).toContain("GET /api/v1/agent/tokens/org"));
    await screen.findByText("Your agent tokens");
    // let the 403 response settle through SWR before asserting
    await act(async () => { await new Promise((r) => setTimeout(r, 100)); });
    expect(screen.queryByText("Organization agent tokens")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("F-619-500: a failed org list shows the error, not the empty state", async () => {
    orgStatus = 500;
    renderWithSWR(<AgentTokensPage />);
    const section = await screen.findByRole("region", { name: "Organization agent tokens" });
    expect(await within(section).findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("No one in your organization has created an agent token yet.")).toBeNull();
  });

  it("F-619-STALE-A: revoking my own token in 'Your agent tokens' updates the org row", async () => {
    renderWithSWR(<AgentTokensPage />);
    const org = await screen.findByRole("region", { name: "Organization agent tokens" });
    await within(org).findByTestId("agent-token-5");
    const mine = screen.getByRole("region", { name: "Your agent tokens" });
    fireEvent.click(await within(mine).findByRole("button", { name: "Revoke My bot" }));
    fireEvent.click(await screen.findByRole("button", { name: "Revoke token" }));
    await waitFor(() => expect(within(within(org).getByTestId("agent-token-5")).getByText("Revoked")).toBeInTheDocument());
  });

  it("F-619-STALE-B: revoking my own token in the org table updates the personal row", async () => {
    renderWithSWR(<AgentTokensPage />);
    const org = await screen.findByRole("region", { name: "Organization agent tokens" });
    const mine = screen.getByRole("region", { name: "Your agent tokens" });
    const mineRow = await within(mine).findByTestId("agent-token-5");
    expect(within(mineRow).getByRole("button", { name: "Lower access for My bot" })).toBeInTheDocument();
    fireEvent.click(await within(org).findByRole("button", { name: "Revoke My bot owned by d@x.io" }));
    fireEvent.click(await screen.findByRole("button", { name: "Revoke token" }));
    await waitFor(() => expect(within(within(mine).getByTestId("agent-token-5")).getByText("Revoked")).toBeInTheDocument());
    expect(within(mine).queryByRole("button", { name: "Lower access for My bot" })).toBeNull();
  });

  it("F-619-DOWNGRADED: an admin of a non-entitled org still gets the org section", async () => {
    ownEmpty = true; // no own tokens, so showTokens is false: only the role may mount the section
    vi.mocked(useAiStatus).mockReturnValue({ ...STATUS, agent: { entitled: false, configured: true } });
    renderWithSWR(<AgentTokensPage />);
    const section = await screen.findByRole("region", { name: "Organization agent tokens" });
    expect(await within(section).findByTestId("agent-token-77")).toBeInTheDocument();
  });

  it("F-619-EMPTY: an empty org shows the empty state", async () => {
    orgItems = [];
    renderWithSWR(<AgentTokensPage />);
    expect(await screen.findByText("No one in your organization has created an agent token yet.")).toBeInTheDocument();
  });

  it("F-619-AXE: the section is axe clean", async () => {
    renderWithSWR(<AgentTokensPage />);
    const section = await screen.findByRole("region", { name: "Organization agent tokens" });
    await within(section).findByTestId("agent-token-77");
    const result = await axe.run(section, { rules: { "color-contrast": { enabled: false } } });
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.html).join(" | ")}`)).toEqual([]);
  });
});
