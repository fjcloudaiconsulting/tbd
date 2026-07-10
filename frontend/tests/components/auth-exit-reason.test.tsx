import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { AuthProvider, useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, setAccessToken } from "@/lib/api";
import type { User } from "@/lib/types";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn(), setAccessToken: vi.fn() };
});

const TEST_USER = {
  id: 1,
  username: "alice",
  email: "alice@example.com",
  first_name: "Alice",
  last_name: "Tester",
  phone: null,
  avatar_url: null,
  email_verified: true,
  role: "owner",
  org_id: 1,
  org_name: "Test Org",
  billing_cycle_day: 1,
  is_superadmin: false,
  is_active: true,
  mfa_enabled: false,
  subscription_status: null,
  subscription_plan: null,
  trial_end: null,
} as unknown as User;

function Harness() {
  const { user, authExitReason, clearAuthExitReason, logout } = useAuth();
  return (
    <div>
      <div data-testid="user">{user?.email ?? "none"}</div>
      <div data-testid="reason">{authExitReason ?? "null"}</div>
      <button onClick={() => logout().catch(() => {})}>Logout</button>
      <button onClick={() => clearAuthExitReason?.()}>Clear</button>
    </div>
  );
}

describe("AuthProvider — authExitReason", () => {
  const apiFetchMock = vi.mocked(apiFetch);

  beforeEach(() => {
    apiFetchMock.mockReset();
    vi.mocked(setAccessToken).mockReset();
  });

  function mountRestored() {
    apiFetchMock
      .mockResolvedValueOnce({ needs_setup: false } as never)
      .mockResolvedValueOnce({ access_token: "restored-token" } as never)
      .mockResolvedValueOnce(TEST_USER as never);
    render(
      <AuthProvider>
        <Harness />
      </AuthProvider>,
    );
  }

  it("defaults authExitReason to null", async () => {
    mountRestored();
    await waitFor(() =>
      expect(screen.getByTestId("user")).toHaveTextContent(TEST_USER.email),
    );
    expect(screen.getByTestId("reason")).toHaveTextContent("null");
  });

  it("sets reason=expired when apiFetch dispatches auth:unauthenticated", async () => {
    mountRestored();
    await waitFor(() =>
      expect(screen.getByTestId("user")).toHaveTextContent(TEST_USER.email),
    );

    act(() => {
      window.dispatchEvent(new Event("auth:unauthenticated"));
    });

    await waitFor(() =>
      expect(screen.getByTestId("reason")).toHaveTextContent("expired"),
    );
    expect(screen.getByTestId("user")).toHaveTextContent("none");
  });

  it("sets reason=manual on user-initiated logout (and never emits the expired signal)", async () => {
    apiFetchMock
      .mockResolvedValueOnce({ needs_setup: false } as never)
      .mockResolvedValueOnce({ access_token: "restored-token" } as never)
      .mockResolvedValueOnce(TEST_USER as never)
      .mockResolvedValueOnce(undefined as never); // POST /logout
    render(
      <AuthProvider>
        <Harness />
      </AuthProvider>,
    );
    await waitFor(() =>
      expect(screen.getByTestId("user")).toHaveTextContent(TEST_USER.email),
    );

    fireEvent.click(screen.getByText("Logout"));

    await waitFor(() =>
      expect(screen.getByTestId("reason")).toHaveTextContent("manual"),
    );
    expect(screen.getByTestId("user")).toHaveTextContent("none");
  });

  it("clearAuthExitReason resets the reason to null", async () => {
    mountRestored();
    await waitFor(() =>
      expect(screen.getByTestId("user")).toHaveTextContent(TEST_USER.email),
    );
    act(() => {
      window.dispatchEvent(new Event("auth:unauthenticated"));
    });
    await waitFor(() =>
      expect(screen.getByTestId("reason")).toHaveTextContent("expired"),
    );

    fireEvent.click(screen.getByText("Clear"));

    await waitFor(() =>
      expect(screen.getByTestId("reason")).toHaveTextContent("null"),
    );
  });
});
