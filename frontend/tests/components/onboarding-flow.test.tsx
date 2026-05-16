/**
 * Onboarding flow integration tests (L3.3).
 *
 * Verifies:
 *   - First-run user lands on the welcome step.
 *   - Skip-all from welcome calls /onboarding/complete and redirects.
 *   - Stepping through to demo + opting in calls /seed-demo.
 *   - 409 from /seed-demo surfaces a soft note (no error blow-up).
 *   - Tour opt-in sets the sessionStorage flag and finishes onboarding.
 *   - Re-visiting /onboarding when already onboarded redirects to /dashboard.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { User } from "@/lib/types";

import OnboardingPageBody from "@/components/onboarding/OnboardingPageBody";
import { apiFetch, ApiResponseError } from "@/lib/api";
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

const replaceMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: replaceMock }),
  usePathname: () => "/onboarding",
}));

function makeUser(overrides: Partial<User> = {}): User {
  return {
    id: 1, username: "newbie", email: "newbie@example.com",
    first_name: null, last_name: null, phone: null, avatar_url: null,
    email_verified: true,
    role: "owner",
    org_id: 1, org_name: "New Org", billing_cycle_day: 1,
    is_superadmin: false, is_active: true, mfa_enabled: false,
    password_set: true,
    allow_manual_balance_adjustment: false,
    onboarded_at: null,
    subscription_status: null, subscription_plan: null, trial_end: null,
    ...overrides,
  };
}

const refreshMeMock = vi.fn(async () => {});

function setupAuth(user: User | null) {
  vi.mocked(useAuth).mockReturnValue({
    user,
    loading: false,
    needsSetup: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: refreshMeMock,
  } as never);
}

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  replaceMock.mockReset();
  refreshMeMock.mockReset();
  try {
    window.sessionStorage.clear();
  } catch {
    // ignore
  }
});

describe("OnboardingPageBody", () => {
  it("renders the welcome step on mount for an un-onboarded user", () => {
    setupAuth(makeUser());
    render(<OnboardingPageBody />);
    expect(
      screen.getByText(/Better decisions about money start here/i),
    ).toBeInTheDocument();
  });

  it("skipping the wizard fires /onboarding/complete and redirects", async () => {
    setupAuth(makeUser());
    vi.mocked(apiFetch).mockResolvedValue({});
    render(<OnboardingPageBody />);
    fireEvent.click(screen.getByTestId("onboarding-skip-all"));
    await waitFor(() => {
      expect(vi.mocked(apiFetch)).toHaveBeenCalledWith(
        "/api/v1/users/me/onboarding/complete",
        expect.objectContaining({ method: "POST" }),
      );
    });
    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/dashboard");
    });
  });

  it("opting into demo data calls /seed-demo and advances", async () => {
    setupAuth(makeUser());
    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url === "/api/v1/account-types") {
        return Promise.resolve([{ id: 10, name: "Checking", slug: "checking" }]);
      }
      return Promise.resolve({});
    }) as never);

    render(<OnboardingPageBody />);
    fireEvent.click(screen.getByTestId("onboarding-next"));
    // Skip the account step to reach demo quickly.
    await waitFor(() =>
      expect(screen.getByTestId("onboarding-skip")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId("onboarding-skip"));
    await waitFor(() =>
      expect(screen.getByTestId("onboarding-accept-seed")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId("onboarding-accept-seed"));
    await waitFor(() => {
      expect(vi.mocked(apiFetch)).toHaveBeenCalledWith(
        "/api/v1/users/me/onboarding/seed-demo",
        expect.objectContaining({ method: "POST" }),
      );
    });
  });

  it("shows a soft note (not a blocking error) when /seed-demo returns 409", async () => {
    setupAuth(makeUser());
    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url === "/api/v1/account-types") {
        return Promise.resolve([{ id: 10, name: "Checking", slug: "checking" }]);
      }
      if (url === "/api/v1/users/me/onboarding/seed-demo") {
        return Promise.reject(new ApiResponseError(409, "org_has_data"));
      }
      return Promise.resolve({});
    }) as never);

    render(<OnboardingPageBody />);
    fireEvent.click(screen.getByTestId("onboarding-next"));
    await waitFor(() =>
      expect(screen.getByTestId("onboarding-skip")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId("onboarding-skip"));
    await waitFor(() =>
      expect(screen.getByTestId("onboarding-accept-seed")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId("onboarding-accept-seed"));
    await waitFor(() => {
      // The 409 path keeps the user on the demo step with the soft
      // note visible. They can skip forward themselves once they have
      // read why we declined.
      expect(screen.getByTestId("onboarding-seed-note")).toBeInTheDocument();
      expect(
        screen.getByText(/your account already has data/i),
      ).toBeInTheDocument();
      expect(replaceMock).not.toHaveBeenCalledWith(
        expect.stringMatching(/^\/dashboard/),
      );
    });
  });

  it("redirects already-onboarded users straight to /dashboard", () => {
    setupAuth(makeUser({ onboarded_at: "2026-05-12T10:00:00" }));
    render(<OnboardingPageBody />);
    expect(replaceMock).toHaveBeenCalledWith("/dashboard");
  });

  it("opting into the tour sets the sessionStorage flag", async () => {
    setupAuth(makeUser());
    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url === "/api/v1/account-types") {
        return Promise.resolve([{ id: 10, name: "Checking", slug: "checking" }]);
      }
      return Promise.resolve({});
    }) as never);

    render(<OnboardingPageBody />);
    fireEvent.click(screen.getByTestId("onboarding-next"));
    await waitFor(() =>
      expect(screen.getByTestId("onboarding-skip")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId("onboarding-skip"));
    await waitFor(() =>
      expect(screen.getByTestId("onboarding-decline-seed")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId("onboarding-decline-seed"));
    await waitFor(() =>
      expect(screen.getByTestId("onboarding-accept-tour")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId("onboarding-accept-tour"));
    await waitFor(() => {
      expect(window.sessionStorage.getItem("tbd-pending-dashboard-tour")).toBe(
        "1",
      );
      expect(replaceMock).toHaveBeenCalledWith("/dashboard");
    });
  });

  it("hides the demo data step for non-owner users (admin)", async () => {
    setupAuth(makeUser({ role: "admin" }));
    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url === "/api/v1/account-types") {
        return Promise.resolve([{ id: 10, name: "Checking", slug: "checking" }]);
      }
      return Promise.resolve({});
    }) as never);

    render(<OnboardingPageBody />);
    // Welcome step shows 3 of 3 (not 4 of 4) because the demo step is
    // not part of this user's wizard.
    expect(screen.getByText(/Step 1 of 3/)).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("onboarding-next"));
    // Skip the account step.
    await waitFor(() =>
      expect(screen.getByTestId("onboarding-skip")).toBeInTheDocument(),
    );
    expect(screen.getByText(/Step 2 of 3/)).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("onboarding-skip"));

    // We should land directly on the tour offer, skipping demo entirely.
    await waitFor(() =>
      expect(screen.getByTestId("onboarding-accept-tour")).toBeInTheDocument(),
    );
    expect(screen.getByText(/Step 3 of 3/)).toBeInTheDocument();
    expect(screen.queryByTestId("onboarding-accept-seed")).not.toBeInTheDocument();
    expect(screen.queryByTestId("onboarding-decline-seed")).not.toBeInTheDocument();
    expect(screen.queryByText(/Yes, add sample data/i)).not.toBeInTheDocument();
  });

  it("hides the demo data step for non-owner users (member)", async () => {
    setupAuth(makeUser({ role: "member" }));
    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url === "/api/v1/account-types") {
        return Promise.resolve([{ id: 10, name: "Checking", slug: "checking" }]);
      }
      return Promise.resolve({});
    }) as never);

    render(<OnboardingPageBody />);
    expect(screen.getByText(/Step 1 of 3/)).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("onboarding-next"));
    await waitFor(() =>
      expect(screen.getByTestId("onboarding-skip")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId("onboarding-skip"));

    await waitFor(() =>
      expect(screen.getByTestId("onboarding-accept-tour")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("onboarding-accept-seed")).not.toBeInTheDocument();
  });

  it("shows the demo data step for owners (sanity-check the role gate)", async () => {
    setupAuth(makeUser({ role: "owner" }));
    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url === "/api/v1/account-types") {
        return Promise.resolve([{ id: 10, name: "Checking", slug: "checking" }]);
      }
      return Promise.resolve({});
    }) as never);

    render(<OnboardingPageBody />);
    expect(screen.getByText(/Step 1 of 4/)).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("onboarding-next"));
    await waitFor(() =>
      expect(screen.getByTestId("onboarding-skip")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId("onboarding-skip"));

    await waitFor(() =>
      expect(screen.getByTestId("onboarding-accept-seed")).toBeInTheDocument(),
    );
    expect(screen.getByText(/Step 3 of 4/)).toBeInTheDocument();
  });

  // L1.1 L4 contract: ``POST /api/v1/accounts`` no longer accepts the
  // free-form ``balance`` field. The audited ``opening_balance`` field
  // is the sole entry point for a starting balance. Pin the onboarding
  // create-account payload to the consolidated shape so the flow
  // does not regress the moment ``AccountCreate`` flips to
  // ``extra="forbid"``.
  it("account-create step posts opening_balance, not balance", async () => {
    setupAuth(makeUser());
    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url === "/api/v1/account-types") {
        return Promise.resolve([{ id: 10, name: "Checking", slug: "checking" }]);
      }
      return Promise.resolve({});
    }) as never);

    render(<OnboardingPageBody />);
    fireEvent.click(screen.getByTestId("onboarding-next"));
    await waitFor(() =>
      expect(
        screen.getByTestId("onboarding-create-account"),
      ).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId("onboarding-create-account"));

    await waitFor(() => {
      expect(vi.mocked(apiFetch)).toHaveBeenCalledWith(
        "/api/v1/accounts",
        expect.objectContaining({ method: "POST" }),
      );
    });

    const createCall = vi
      .mocked(apiFetch)
      .mock.calls.find(([url]) => url === "/api/v1/accounts");
    expect(createCall).toBeDefined();
    const body = JSON.parse(
      (createCall![1] as { body: string }).body,
    ) as Record<string, unknown>;

    // The whole point: NO ``balance`` key, and the explicit
    // ``opening_balance: "0.00"`` is preserved so the audited path is
    // exercised even on the zero-amount onboarding default.
    expect(body).not.toHaveProperty("balance");
    expect(body).toEqual(
      expect.objectContaining({
        name: expect.any(String),
        account_type_id: 10,
        currency: "EUR",
        opening_balance: "0.00",
      }),
    );
  });
});
