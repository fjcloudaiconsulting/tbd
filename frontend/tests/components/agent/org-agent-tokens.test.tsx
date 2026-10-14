/**
 * TBD-619 org admin view of members' agent tokens: admin sees rows, a member
 * never fetches the org route, Revoke hits the ORG delete route, a 403 renders
 * nothing broken, axe clean.
 */
import React from "react";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import axe from "axe-core";

import AgentTokensPage from "@/app/settings/agent-tokens/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { setAccessToken } from "@/lib/api";
import { useAiStatus } from "@/lib/hooks/use-ai-status";
import type { OrgAgentToken } from "@/lib/agent/types";
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
let orgStatus: number;
let fetchMock: ReturnType<typeof vi.fn>;
const calls = () => fetchMock.mock.calls.map(([u, i]) => `${(i as RequestInit)?.method ?? "GET"} ${u}`);

beforeEach(() => {
  setAccessToken("t");
  orgItems = [orgToken({})];
  orgStatus = 200;
  vi.mocked(useAuth).mockReturnValue({ user: user("admin"), loading: false } as unknown as ReturnType<typeof useAuth>);
  vi.mocked(useAiStatus).mockReturnValue(STATUS);
  fetchMock = vi.fn(async (url: string, init: RequestInit = {}) => {
    const key = `${init.method ?? "GET"} ${String(url)}`;
    if (key === "GET /api/v1/agent/tokens") return list([]);
    if (key === "GET /api/v1/agent/actions?status=done&limit=20") return list([]);
    if (key === "GET /api/v1/agent/tokens/org") {
      return orgStatus === 200 ? list(orgItems) : json(orgStatus, { detail: "Forbidden" });
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
    expect(screen.queryByText("Organization agent tokens")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
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
