import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import OrganizationSettingsPage from "@/app/settings/organization/page";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";
import { mutate } from "swr";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("swr", async () => {
  const actual = await vi.importActual<typeof import("swr")>("swr");
  return { ...actual, mutate: vi.fn(() => Promise.resolve()) };
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
const replaceMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, replace: replaceMock }),
  usePathname: () => "/settings/organization",
}));

const ORG_NAME = "Acme Household";

function makeUser(role: "owner" | "admin" | "member") {
  return {
    id: 1, username: "u", email: "u@x.io",
    first_name: null, last_name: null, phone: null, avatar_url: null,
    email_verified: true,
    role,
    org_id: 1, org_name: ORG_NAME, billing_cycle_day: 1,
    is_superadmin: false, is_active: true, mfa_enabled: false,
    subscription_status: null, subscription_plan: null, trial_end: null,
  };
}

function mockApiSuccessFixtures() {
  vi.mocked(apiFetch).mockImplementation(((url: string, init?: RequestInit) => {
    if (url === "/api/v1/settings/billing-cycle") return Promise.resolve({ billing_cycle_day: 1 });
    if (url === "/api/v1/settings/billing-period") return Promise.resolve({ id: 1, start_date: "2026-05-01", end_date: null });
    if (url === "/api/v1/settings") return Promise.resolve([]);
    if (url === "/api/v1/orgs/members") return Promise.resolve([]);
    if (url === "/api/v1/orgs/invitations") return Promise.resolve([]);
    if (url === "/api/v1/category-rules") return Promise.resolve([]);
    if (url === "/api/v1/orgs/data/reset" && init?.method === "POST") {
      return Promise.resolve({ deleted_rows_by_table: { transactions: 0 } });
    }
    return Promise.resolve({});
  }) as never);
}

function mockUser(role: "owner" | "admin" | "member") {
  vi.mocked(useAuth).mockReturnValue({
    user: makeUser(role) as never,
    loading: false,
    needsSetup: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn(),
  } as never);
}

describe("OrganizationSettingsPage — Danger Zone", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    pushMock.mockReset();
    replaceMock.mockReset();
    vi.mocked(mutate).mockClear();
    mockApiSuccessFixtures();
  });

  it("does not render Danger Zone for member role", async () => {
    mockUser("member");
    render(<OrganizationSettingsPage />);
    // Member should be redirected away from this page entirely (admin-only),
    // so the Danger Zone never appears.
    await waitFor(() => expect(screen.queryByText(/Danger zone/i)).toBeNull());
  });

  it("does not render Danger Zone for admin role", async () => {
    mockUser("admin");
    render(<OrganizationSettingsPage />);
    await waitFor(() => expect(screen.queryByText(/Danger zone/i)).toBeNull());
  });

  it("renders Danger Zone for owner role", async () => {
    mockUser("owner");
    render(<OrganizationSettingsPage />);
    await waitFor(() => expect(screen.getByText(/Danger zone/i)).toBeInTheDocument());
  });

  it("disables Reset button until confirm phrase exactly matches", async () => {
    mockUser("owner");
    render(<OrganizationSettingsPage />);
    await waitFor(() => expect(screen.getByText(/Danger zone/i)).toBeInTheDocument());

    const button = screen.getByRole("button", { name: /reset organization data permanently/i }) as HTMLButtonElement;
    const input = screen.getByLabelText(/confirm reset phrase/i);

    expect(button.disabled).toBe(true);

    // Wrong case
    fireEvent.change(input, { target: { value: `reset ${ORG_NAME.toLowerCase()}` } });
    expect(button.disabled).toBe(true);

    // Wrong name
    fireEvent.change(input, { target: { value: "RESET Wrong" } });
    expect(button.disabled).toBe(true);

    // Just RESET
    fireEvent.change(input, { target: { value: "RESET" } });
    expect(button.disabled).toBe(true);

    // Exact match
    fireEvent.change(input, { target: { value: `RESET ${ORG_NAME}` } });
    expect(button.disabled).toBe(false);

    // Trimmed exact match
    fireEvent.change(input, { target: { value: `   RESET ${ORG_NAME}    ` } });
    expect(button.disabled).toBe(false);
  });

  it("POSTs the correct phrase and redirects to /dashboard?reset=1 on success", async () => {
    mockUser("owner");
    render(<OrganizationSettingsPage />);
    await waitFor(() => expect(screen.getByText(/Danger zone/i)).toBeInTheDocument());

    const input = screen.getByLabelText(/confirm reset phrase/i);
    fireEvent.change(input, { target: { value: `RESET ${ORG_NAME}` } });
    fireEvent.click(screen.getByRole("button", { name: /reset organization data permanently/i }));

    await waitFor(() => {
      const call = vi.mocked(apiFetch).mock.calls.find(
        ([url]) => url === "/api/v1/orgs/data/reset",
      );
      expect(call).toBeTruthy();
      const init = call![1] as RequestInit;
      expect(init?.method).toBe("POST");
      expect(JSON.parse(init!.body as string)).toEqual({
        confirm_phrase: `RESET ${ORG_NAME}`,
      });
    });
    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/dashboard?reset=1"));
  });

  it("clears every SWR cache key on reset success before navigating", async () => {
    mockUser("owner");
    render(<OrganizationSettingsPage />);
    await waitFor(() => expect(screen.getByText(/Danger zone/i)).toBeInTheDocument());

    const input = screen.getByLabelText(/confirm reset phrase/i);
    fireEvent.change(input, { target: { value: `RESET ${ORG_NAME}` } });
    fireEvent.click(screen.getByRole("button", { name: /reset organization data permanently/i }));

    await waitFor(() => expect(vi.mocked(mutate)).toHaveBeenCalled());
    // Match-all matcher (function predicate), undefined value (clear),
    // revalidate: false (don't refetch — we're navigating away).
    const call = vi.mocked(mutate).mock.calls[0];
    expect(typeof call[0]).toBe("function");
    expect(call[1]).toBeUndefined();
    expect(call[2]).toEqual({ revalidate: false });
    // mutate must be invoked before the redirect, so any subsequent
    // hook on the destination page sees a clean cache.
    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/dashboard?reset=1"));
    const mutateOrder = vi.mocked(mutate).mock.invocationCallOrder[0];
    const pushOrder = pushMock.mock.invocationCallOrder[0];
    expect(mutateOrder).toBeLessThan(pushOrder);
  });

  it("shows a spinner + 'Resetting organization data...' while the POST is in flight", async () => {
    mockUser("owner");
    // Hold the POST open so we can assert the in-flight UI. Wire up
    // the same baseline fixtures the other tests use, then override
    // /orgs/data/reset to be deferred.
    let resolveReset!: (v: unknown) => void;
    const resetPromise = new Promise((r) => {
      resolveReset = r;
    });
    vi.mocked(apiFetch).mockImplementation(((url: string, init?: RequestInit) => {
      if (url === "/api/v1/orgs/data/reset" && init?.method === "POST")
        return resetPromise as never;
      if (url === "/api/v1/settings/billing-cycle")
        return Promise.resolve({ billing_cycle_day: 1 });
      if (url === "/api/v1/settings/billing-period")
        return Promise.resolve({ id: 1, start_date: "2026-05-01", end_date: null });
      if (url === "/api/v1/settings") return Promise.resolve([]);
      if (url === "/api/v1/orgs/members") return Promise.resolve([]);
      if (url === "/api/v1/orgs/invitations") return Promise.resolve([]);
      if (url === "/api/v1/category-rules") return Promise.resolve([]);
      return Promise.resolve({});
    }) as never);
    render(<OrganizationSettingsPage />);
    await waitFor(() => expect(screen.getByText(/Danger zone/i)).toBeInTheDocument());

    const input = screen.getByLabelText(/confirm reset phrase/i);
    fireEvent.change(input, { target: { value: `RESET ${ORG_NAME}` } });
    const button = screen.getByRole("button", { name: /reset organization data permanently/i });
    fireEvent.click(button);

    // In-flight: button copy switches to "Resetting organization data..."
    // and a spinner svg appears next to it.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /resetting organization data/i })).toBeInTheDocument(),
    );
    const spinningButton = screen.getByRole("button", { name: /resetting organization data/i });
    // The spinner is a Loader2 lucide icon with the animate-spin class.
    const spinner = spinningButton.querySelector("svg");
    expect(spinner).not.toBeNull();
    expect(spinner?.classList.contains("animate-spin")).toBe(true);
    // Button is disabled while the POST is in flight.
    expect((spinningButton as HTMLButtonElement).disabled).toBe(true);

    // Resolve the POST so the test cleans up.
    resolveReset({});
  });

  it("surfaces the 409 'reset_already_running' error message and re-enables the button", async () => {
    mockUser("owner");
    // Mirror apiFetch's ApiResponseError shape — the wrapper translates
    // the structured 409 detail into err.message.
    class FakeApiResponseError extends Error {
      status = 409;
      code = "reset_already_running";
      constructor(message: string) {
        super(message);
        this.name = "ApiResponseError";
      }
    }
    vi.mocked(apiFetch).mockImplementation(((url: string, init?: RequestInit) => {
      if (url === "/api/v1/orgs/data/reset" && init?.method === "POST") {
        return Promise.reject(
          new FakeApiResponseError(
            "Another reset is already running for this organization. Please wait a moment and try again.",
          ),
        );
      }
      if (url === "/api/v1/settings/billing-cycle")
        return Promise.resolve({ billing_cycle_day: 1 });
      if (url === "/api/v1/settings/billing-period")
        return Promise.resolve({ id: 1, start_date: "2026-05-01", end_date: null });
      if (url === "/api/v1/settings") return Promise.resolve([]);
      if (url === "/api/v1/orgs/members") return Promise.resolve([]);
      if (url === "/api/v1/orgs/invitations") return Promise.resolve([]);
      if (url === "/api/v1/category-rules") return Promise.resolve([]);
      return Promise.resolve({});
    }) as never);
    render(<OrganizationSettingsPage />);
    await waitFor(() => expect(screen.getByText(/Danger zone/i)).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText(/confirm reset phrase/i), {
      target: { value: `RESET ${ORG_NAME}` },
    });
    fireEvent.click(screen.getByRole("button", { name: /reset organization data permanently/i }));

    // Error banner shows the structured message from the backend.
    await waitFor(() =>
      expect(screen.getByText(/Another reset is already running/)).toBeInTheDocument(),
    );

    // Button returns to its enabled-with-default-copy state so the
    // user can retry once the other reset finishes.
    const button = screen.getByRole("button", {
      name: /reset organization data permanently/i,
    }) as HTMLButtonElement;
    expect(button.disabled).toBe(false);
    // Spinner copy is gone.
    expect(screen.queryByText(/Resetting organization data/)).toBeNull();
  });
});
