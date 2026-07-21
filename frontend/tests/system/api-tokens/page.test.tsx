import React from "react";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";

import { renderWithSWR } from "@/tests/utils/render-with-swr";
import SystemApiTokensPage from "@/app/system/api-tokens/page";
import {
  listApiTokens,
  mintApiToken,
  revokeApiToken,
  revokeAllApiTokens,
} from "@/lib/api-tokens";
import { useAuth } from "@/components/auth/AuthProvider";
import type { ApiToken, MintTokenResponse, User } from "@/lib/types";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/system/api-tokens",
}));

// AppShell pulls in the whole authed chrome; a passthrough keeps this suite
// focused on the page body. The superadmin guard is exercised via useAuth.
vi.mock("@/components/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app-shell">{children}</div>
  ),
}));

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<
    typeof import("@/components/auth/AuthProvider")
  >("@/components/auth/AuthProvider");
  return { ...actual, useAuth: vi.fn() };
});

vi.mock("@/lib/api-tokens", () => ({
  API_TOKENS_BASE: "/api/v1/system/api-tokens",
  listApiTokens: vi.fn(),
  mintApiToken: vi.fn(),
  revokeApiToken: vi.fn(),
  revokeAllApiTokens: vi.fn(),
}));

const listMock = vi.mocked(listApiTokens);
const mintMock = vi.mocked(mintApiToken);
const revokeMock = vi.mocked(revokeApiToken);
const revokeAllMock = vi.mocked(revokeAllApiTokens);
const useAuthMock = vi.mocked(useAuth);

function makeUser(overrides: Partial<User> = {}): User {
  return {
    id: 1,
    username: "root",
    email: "root@x.io",
    first_name: null,
    last_name: null,
    phone: null,
    avatar_url: null,
    email_verified: true,
    role: "owner",
    org_id: 1,
    org_name: "HQ",
    billing_cycle_day: 1,
    is_superadmin: true,
    is_active: true,
    mfa_enabled: false,
    password_set: true,
    allow_manual_balance_adjustment: false,
    subscription_status: null,
    subscription_plan: null,
    trial_end: null,
    ...overrides,
  };
}

function mockAuth(user: User | null, loading = false) {
  useAuthMock.mockReturnValue({
    user,
    loading,
  } as unknown as ReturnType<typeof useAuth>);
}

const TOKEN: ApiToken = {
  id: 1,
  name: "broadcast cron",
  prefix: "pat_a1b2c3",
  scope: "read",
  created_at: "2026-07-01T00:00:00Z",
  expires_at: "2026-12-01T00:00:00Z",
  last_used_at: null,
  status: "active",
};

const MINTED: MintTokenResponse = {
  token: "pat_supersecretplaintexttokenvalue000",
  id: 2,
  name: "local scripting",
  prefix: "pat_zzzz11",
  scope: "write",
  created_at: "2026-07-21T12:00:00Z",
  expires_at: "2026-10-19T12:00:00Z",
};

beforeEach(() => {
  listMock.mockReset();
  mintMock.mockReset();
  revokeMock.mockReset();
  revokeAllMock.mockReset();
  useAuthMock.mockReset();
  listMock.mockResolvedValue({ items: [TOKEN], total: 1, limit: 1, offset: 0 });
  mockAuth(makeUser());
});

describe("/system/api-tokens page", () => {
  it("lists the caller's tokens", async () => {
    renderWithSWR(<SystemApiTokensPage />);
    await screen.findByText("broadcast cron");
    expect(screen.getByTestId("api-token-row-1")).toBeInTheDocument();
  });

  it("states plainly that PATs survive password change and session invalidation", async () => {
    renderWithSWR(<SystemApiTokensPage />);
    const security = await screen.findByTestId("api-token-security-copy");
    expect(security).toHaveTextContent(/password change/i);
    expect(security).toHaveTextContent(/sign(ing)? out everywhere|session invalidation/i);
    expect(security).toHaveTextContent(/revoke|expir/i);
    // The all-org read warning.
    expect(security).toHaveTextContent(/read.*(all|every).*org/i);
  });

  it("shows inline usage help with curl, the write-POST caveat, and the optional-auth note", async () => {
    renderWithSWR(<SystemApiTokensPage />);
    const help = await screen.findByTestId("usage-help");
    expect(help).toHaveTextContent(/curl/i);
    expect(help).toHaveTextContent(/Authorization: Bearer pat_/);
    expect(help).toHaveTextContent(/some POST|POST endpoints|reads.*write/i);
    expect(help).toHaveTextContent(/optional-auth|public endpoints/i);
  });

  it("mints a token through the step-up modal and reveals it once", async () => {
    mintMock.mockResolvedValue(MINTED);
    renderWithSWR(<SystemApiTokensPage />);
    await screen.findByText("broadcast cron");

    fireEvent.change(screen.getByTestId("mint-name"), {
      target: { value: "local scripting" },
    });
    fireEvent.click(screen.getByTestId("mint-scope-write"));
    fireEvent.submit(screen.getByTestId("mint-form"));

    // Step-up modal (password_set user, no MFA) collects the password.
    const modal = await screen.findByTestId("stepup-modal");
    fireEvent.change(within(modal).getByTestId("stepup-password"), {
      target: { value: "hunter2" },
    });
    fireEvent.click(within(modal).getByTestId("stepup-submit"));

    await waitFor(() =>
      expect(mintMock).toHaveBeenCalledWith({
        name: "local scripting",
        scope: "write",
        expires_in_days: 30,
        current_password: "hunter2",
      }),
    );

    const panel = await screen.findByTestId("reveal-panel");
    expect(within(panel).getByTestId("reveal-token")).toHaveTextContent(MINTED.token);
  });

  it("keeps the step-up modal open and shows the error on a failed proof", async () => {
    const { ApiResponseError } = await vi.importActual<typeof import("@/lib/api")>(
      "@/lib/api",
    );
    mintMock.mockRejectedValue(
      new ApiResponseError(401, "Step-up verification required"),
    );
    renderWithSWR(<SystemApiTokensPage />);
    await screen.findByText("broadcast cron");

    fireEvent.change(screen.getByTestId("mint-name"), {
      target: { value: "x" },
    });
    fireEvent.submit(screen.getByTestId("mint-form"));
    const modal = await screen.findByTestId("stepup-modal");
    fireEvent.change(within(modal).getByTestId("stepup-password"), {
      target: { value: "wrong" },
    });
    fireEvent.click(within(modal).getByTestId("stepup-submit"));

    await screen.findByTestId("stepup-error");
    expect(screen.getByTestId("stepup-modal")).toBeInTheDocument();
    expect(screen.queryByTestId("reveal-panel")).not.toBeInTheDocument();
  });

  it("revokes a single token after confirmation via the shared ConfirmModal", async () => {
    revokeMock.mockResolvedValue({ ok: true, id: 1 });
    renderWithSWR(<SystemApiTokensPage />);
    await screen.findByText("broadcast cron");

    fireEvent.click(screen.getByTestId("api-token-revoke-1"));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent(/revoke token/i);
    fireEvent.click(within(dialog).getByRole("button", { name: "Revoke token" }));
    await waitFor(() => expect(revokeMock).toHaveBeenCalledWith(1));
  });

  it("does not revoke a single token when the confirm modal is cancelled", async () => {
    renderWithSWR(<SystemApiTokensPage />);
    await screen.findByText("broadcast cron");

    fireEvent.click(screen.getByTestId("api-token-revoke-1"));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(revokeMock).not.toHaveBeenCalled();
  });

  it("revokes all tokens from the panic button via the shared ConfirmModal", async () => {
    revokeAllMock.mockResolvedValue({ revoked: 3 });
    renderWithSWR(<SystemApiTokensPage />);
    await screen.findByText("broadcast cron");

    fireEvent.click(screen.getByTestId("revoke-all-button"));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent(/revoke all tokens/i);
    fireEvent.click(within(dialog).getByRole("button", { name: "Revoke all" }));
    await waitFor(() => expect(revokeAllMock).toHaveBeenCalled());
  });

  it("does not revoke all tokens when the confirm modal is cancelled", async () => {
    renderWithSWR(<SystemApiTokensPage />);
    await screen.findByText("broadcast cron");

    fireEvent.click(screen.getByTestId("revoke-all-button"));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(revokeAllMock).not.toHaveBeenCalled();
  });

  it("disables revoke-all when there are no active tokens", async () => {
    listMock.mockResolvedValue({
      items: [{ ...TOKEN, id: 2, status: "revoked" }],
      total: 1,
      limit: 1,
      offset: 0,
    });
    renderWithSWR(<SystemApiTokensPage />);
    await screen.findByText("broadcast cron");

    expect(screen.getByTestId("revoke-all-button")).toBeDisabled();
  });

  it("enables revoke-all when at least one token is active", async () => {
    listMock.mockResolvedValue({
      items: [
        { ...TOKEN, id: 2, name: "dead cron", status: "revoked" },
        { ...TOKEN, id: 3, name: "live cron", status: "active" },
      ],
      total: 2,
      limit: 2,
      offset: 0,
    });
    renderWithSWR(<SystemApiTokensPage />);
    await screen.findByText("live cron");

    expect(screen.getByTestId("revoke-all-button")).not.toBeDisabled();
  });
});
