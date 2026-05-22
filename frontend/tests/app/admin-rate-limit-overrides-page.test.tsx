import { render, screen, waitFor, fireEvent } from "@testing-library/react";

import AdminRateLimitOverridesPage from "@/app/admin/rate-limit-overrides/page";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";

// Mocks mirror the audit page test shape so the gate, fetch, and
// router redirect plumbing reuse the same assertions.

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
  usePathname: () => "/admin/rate-limit-overrides",
}));

const SUPERADMIN = {
  id: 1,
  username: "root",
  email: "root@platform.io",
  first_name: null,
  last_name: null,
  phone: null,
  avatar_url: null,
  email_verified: true,
  role: "owner",
  org_id: 1,
  org_name: "Platform",
  billing_cycle_day: 1,
  is_superadmin: true,
  is_active: true,
  mfa_enabled: false,
  subscription_status: null,
  subscription_plan: null,
  trial_end: null,
};

const LIST_RESPONSE = {
  items: [
    {
      id: 7,
      org_id: 42,
      user_id: null,
      endpoint_pattern: "auth.login",
      max_requests: 100,
      period_seconds: 60,
      expires_at: null,
      created_by_user_id: 1,
      note: "B2B ramp",
      created_at: "2026-05-22T09:00:00Z",
      updated_at: "2026-05-22T09:00:00Z",
    },
  ],
  total: 1,
};

const CATALOGUE_RESPONSE = {
  overridable: ["accounts.adjust_balance", "reports.query"],
  pre_auth_informational: ["auth.login", "auth.register"],
};

// Helper: every URL the page fetches gets the canned response so
// the order of effects doesn't matter for the test. The page fires
// both /endpoint-catalogue and the list URL in parallel, and
// neither orders deterministically against React's effect queue.
function defaultApiFetchMock(url: string) {
  if (String(url).includes("/endpoint-catalogue")) {
    return Promise.resolve(CATALOGUE_RESPONSE);
  }
  return Promise.resolve(LIST_RESPONSE);
}

describe("AdminRateLimitOverridesPage", () => {
  const apiFetchMock = vi.mocked(apiFetch);
  const useAuthMock = vi.mocked(useAuth);

  beforeEach(() => {
    apiFetchMock.mockReset();
    // Default: every URL the page fetches resolves with the canned
    // response (catalogue or list). Individual tests can still chain
    // ``mockImplementationOnce`` if they need a specific sequence.
    apiFetchMock.mockImplementation(((url: string) =>
      defaultApiFetchMock(url)) as never);
    replaceMock.mockReset();
    useAuthMock.mockReturnValue({
      user: SUPERADMIN as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });
  });

  it("renders overrides for a superadmin", async () => {
    render(<AdminRateLimitOverridesPage />);

    await screen.findByText("auth.login");
    expect(screen.getByText("Org #42")).toBeInTheDocument();
    expect(screen.getByText("100")).toBeInTheDocument();
    expect(screen.getByText("minute")).toBeInTheDocument();
    expect(replaceMock).not.toHaveBeenCalledWith("/dashboard");
  });

  it("redirects non-superadmin users away from the page", async () => {
    useAuthMock.mockReturnValue({
      user: { ...SUPERADMIN, is_superadmin: false } as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    render(<AdminRateLimitOverridesPage />);

    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/dashboard");
    });
  });

  it("opens the add modal when Add override is clicked", async () => {
    render(<AdminRateLimitOverridesPage />);

    await screen.findByText("auth.login");
    fireEvent.click(screen.getByRole("button", { name: /Add override/i }));
    expect(
      await screen.findByRole("heading", { name: /New override/i }),
    ).toBeInTheDocument();
    // ``Org id`` matches both the filter input and the form scope
    // input; assert the form-specific input by id.
    expect(document.getElementById("scope-id")).not.toBeNull();
  });

  it("opens the edit modal pre-populated with the row's values", async () => {
    render(<AdminRateLimitOverridesPage />);

    await screen.findByText("auth.login");
    fireEvent.click(screen.getByRole("button", { name: /Edit/i }));
    expect(
      await screen.findByRole("heading", { name: /Edit override #7/i }),
    ).toBeInTheDocument();
    const maxInput = screen.getByLabelText(/Max requests/i) as HTMLInputElement;
    expect(maxInput.value).toBe("100");
  });

  it("opens the delete confirm dialog and POSTs DELETE on confirm", async () => {
    render(<AdminRateLimitOverridesPage />);

    await screen.findByText("auth.login");
    fireEvent.click(screen.getByRole("button", { name: /Delete/i }));
    expect(
      await screen.findByRole("heading", { name: /Delete override #7/i }),
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("dialog").querySelector(
        'button.text-danger-strong, button[class*="Danger"]',
      ) ?? screen.getAllByRole("button", { name: /Delete/i })[1],
    );
    await waitFor(() => {
      const deleteCall = apiFetchMock.mock.calls.find(
        (c) => typeof c[1] === "object" && (c[1] as RequestInit | undefined)?.method === "DELETE",
      );
      expect(deleteCall).toBeTruthy();
      expect(String(deleteCall![0])).toContain("/api/v1/admin/rate-limit-overrides/7");
    });
  });

  it("filters by endpoint pattern via the search input", async () => {
    render(<AdminRateLimitOverridesPage />);

    await screen.findByText("auth.login");
    fireEvent.change(
      screen.getByPlaceholderText(/Endpoint pattern/i),
      { target: { value: "auth.register" } },
    );
    await waitFor(() => {
      const listCalls = apiFetchMock.mock.calls.filter(
        (c) =>
          String(c[0]).startsWith("/api/v1/admin/rate-limit-overrides?") ||
          String(c[0]).startsWith("/api/v1/admin/rate-limit-overrides?"),
      );
      const lastListCall = listCalls.at(-1);
      expect(lastListCall).toBeTruthy();
      expect(String(lastListCall![0])).toContain(
        "endpoint_pattern=auth.register",
      );
    });
  });

  it("renders the endpoint catalogue items in the form dropdown", async () => {
    render(<AdminRateLimitOverridesPage />);

    await screen.findByText("auth.login");
    fireEvent.click(screen.getByRole("button", { name: /Add override/i }));
    await screen.findByRole("heading", { name: /New override/i });

    // The dropdown shows every catalogue pattern. Overridable patterns
    // are selectable; pre-auth patterns are rendered as DISABLED
    // options under an optgroup so operators can see the full surface
    // but cannot accidentally persist a no-op override.
    const dropdown = screen.getByLabelText(
      "Endpoint pattern",
    ) as HTMLSelectElement;
    const optionValues = Array.from(dropdown.options).map((o) => o.value);
    expect(optionValues).toContain("auth.login");
    expect(optionValues).toContain("auth.register");
    expect(optionValues).toContain("reports.query");
    expect(optionValues).toContain("accounts.adjust_balance");

    // Overridable options are NOT disabled.
    const overridableOption = Array.from(dropdown.options).find(
      (o) => o.value === "reports.query",
    );
    expect(overridableOption?.disabled).toBe(false);

    // Pre-auth options ARE disabled — this is the behavioral guard.
    const preAuthOption = Array.from(dropdown.options).find(
      (o) => o.value === "auth.login",
    );
    expect(preAuthOption?.disabled).toBe(true);
    // Tooltip explains why so the surface stays discoverable.
    expect(preAuthOption?.title).toMatch(/pre-auth/i);

    // Pre-auth options are grouped under an optgroup labelled to
    // explain the disabled state.
    const optgroup = dropdown.querySelector("optgroup");
    expect(optgroup?.getAttribute("label")).toMatch(/pre-auth/i);
  });

  it("surfaces a typed 422 when the backend rejects a pre-auth pattern", async () => {
    // Defensive: if the disabled-in-dropdown gate is somehow bypassed
    // (devtools, stale catalogue, etc.) the backend's typed 422 must
    // still surface to the operator with the right message.
    render(<AdminRateLimitOverridesPage />);

    await screen.findByText("auth.login");
    fireEvent.click(screen.getByRole("button", { name: /Add override/i }));
    await screen.findByRole("heading", { name: /New override/i });

    // Force-pick a pre-auth pattern by setting the select's value
    // directly (operator-bypassed disabled state).
    const dropdown = screen.getByLabelText(
      "Endpoint pattern",
    ) as HTMLSelectElement;
    Object.defineProperty(dropdown, "value", {
      writable: true,
      value: "auth.login",
    });
    fireEvent.change(dropdown, { target: { value: "auth.login" } });
    fireEvent.change(screen.getByLabelText(/^Org id$/i), {
      target: { value: "1" },
    });
    fireEvent.change(screen.getByLabelText(/Max requests/i), {
      target: { value: "10" },
    });
    fireEvent.change(screen.getByLabelText(/Period \(seconds\)/i), {
      target: { value: "60" },
    });

    // Backend rejects the submit with the typed 422.
    apiFetchMock.mockImplementationOnce(() =>
      Promise.reject(
        new Error(
          "endpoint_pattern_pre_auth_non_overridable: endpoint 'auth.login' is a pre-auth route; overrides are not honored. Adjust the slowapi decorator default in code instead.",
        ),
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: /^Save$/i }));

    expect(
      await screen.findByText(/pre-auth route/i),
    ).toBeInTheDocument();
  });

  it("validates that an endpoint pattern is selected before submit", async () => {
    render(<AdminRateLimitOverridesPage />);

    await screen.findByText("auth.login");
    fireEvent.click(screen.getByRole("button", { name: /Add override/i }));
    await screen.findByRole("heading", { name: /New override/i });

    // Leave the dropdown at its default empty value and submit. The
    // page-level guard surfaces the error before any network call.
    // (The native HTML5 ``required`` on the <select> would also
    // block submission, but we additionally surface a friendly
    // message via setSubmitError.)
    const form = screen.getByRole("heading", {
      name: /New override/i,
    }).closest("form");
    expect(form).not.toBeNull();
    // Bypass the browser's built-in required-field gate so we can
    // assert the JS-level validator fires too.
    form!.setAttribute("novalidate", "true");
    // Fill the required numeric fields so the only missing one is
    // the dropdown.
    fireEvent.change(screen.getByLabelText(/^Org id$/i), {
      target: { value: "1" },
    });
    fireEvent.change(screen.getByLabelText(/Max requests/i), {
      target: { value: "10" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/i }));

    expect(
      await screen.findByText(/Pick an endpoint from the catalogue/i),
    ).toBeInTheDocument();
  });

  it("renders the pre-auth limitation callout", async () => {
    render(<AdminRateLimitOverridesPage />);

    await screen.findByText("auth.login");
    expect(
      screen.getByRole("note", { name: /Pre-auth limitation/i }),
    ).toBeInTheDocument();
  });
});
