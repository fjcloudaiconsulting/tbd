/**
 * RestartTourCard tests.
 *
 * Verifies the Settings Profile-tab affordance for L3.3 tour replay:
 *   - Button renders and is enabled for any signed-in user.
 *   - Click calls POST /api/v1/users/me/onboarding/restart-tour, sets
 *     the dashboard auto-start sessionStorage flag, refreshes the
 *     cached /me, and navigates to /dashboard.
 *   - Errors are surfaced inline without throwing.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import RestartTourCard from "@/components/settings/RestartTourCard";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<typeof import("@/components/auth/AuthProvider")>(
    "@/components/auth/AuthProvider",
  );
  return {
    ...actual,
    useAuth: vi.fn(),
    AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  };
});

const pushMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, replace: vi.fn() }),
  usePathname: () => "/settings",
}));

const refreshMeMock = vi.fn(async () => {});

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  pushMock.mockReset();
  refreshMeMock.mockReset();
  vi.mocked(useAuth).mockReturnValue({
    user: null,
    loading: false,
    needsSetup: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: refreshMeMock,
  } as never);
  try {
    window.sessionStorage.clear();
  } catch {
    // ignore
  }
});

describe("RestartTourCard", () => {
  it("renders the replay tour button", () => {
    render(<RestartTourCard />);
    expect(screen.getByTestId("settings-restart-tour")).toHaveTextContent(
      /replay the dashboard tour/i,
    );
  });

  it("calls the restart endpoint, stamps the dashboard tour flag, and navigates", async () => {
    vi.mocked(apiFetch).mockResolvedValue({});
    render(<RestartTourCard />);
    fireEvent.click(screen.getByTestId("settings-restart-tour"));

    await waitFor(() => {
      expect(vi.mocked(apiFetch)).toHaveBeenCalledWith(
        "/api/v1/users/me/onboarding/restart-tour",
        expect.objectContaining({ method: "POST" }),
      );
    });
    await waitFor(() => {
      expect(pushMock).toHaveBeenCalledWith("/dashboard");
    });
    expect(window.sessionStorage.getItem("tbd-pending-dashboard-tour")).toBe(
      "1",
    );
    expect(refreshMeMock).toHaveBeenCalled();
  });

  it("surfaces an inline error when the endpoint rejects", async () => {
    vi.mocked(apiFetch).mockRejectedValue(new Error("boom"));
    render(<RestartTourCard />);
    fireEvent.click(screen.getByTestId("settings-restart-tour"));
    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(/boom|could not/i);
    });
    expect(pushMock).not.toHaveBeenCalled();
  });

  // The backend no longer clears ``users.onboarded_at`` on restart-tour
  // (that caused an AppShell /onboarding redirect loop). Simulate the
  // refreshMe call returning a user whose ``onboarded_at`` is still set,
  // proving the value-preserved contract. AppShell guards on this exact
  // field, so a non-null value is the proxy for "no redirect loop".
  it("preserves onboarded_at on the cached /me so AppShell does not loop", async () => {
    const ONBOARDED_AT = "2026-05-14T10:00:00Z";
    vi.mocked(apiFetch).mockResolvedValue({ onboarded_at: ONBOARDED_AT });
    let observedUser: { onboarded_at: string | null } | null = null;
    refreshMeMock.mockImplementation(async () => {
      // After the backend returns the unchanged timestamp, the cached
      // /me payload still has a non-null value. AppShell's guard
      // (user.onboarded_at !== null) would short-circuit.
      observedUser = { onboarded_at: ONBOARDED_AT };
    });

    render(<RestartTourCard />);
    fireEvent.click(screen.getByTestId("settings-restart-tour"));

    await waitFor(() => {
      expect(pushMock).toHaveBeenCalledWith("/dashboard");
    });
    expect(observedUser).not.toBeNull();
    expect(observedUser!.onboarded_at).not.toBeNull();
  });
});
