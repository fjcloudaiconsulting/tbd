import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import OrganizationSettingsPage from "@/app/settings/organization/page";
import { apiFetch, ApiResponseError } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";

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

function makeUser() {
  return {
    id: 1, username: "u", email: "u@x.io",
    first_name: null, last_name: null, phone: null, avatar_url: null,
    email_verified: true,
    role: "owner" as const,
    org_id: 42, org_name: "Acme", billing_cycle_day: 1,
    is_superadmin: false, is_active: true, mfa_enabled: false,
    password_set: true,
    subscription_status: null, subscription_plan: null, trial_end: null,
  };
}

function baseFixtures() {
  vi.mocked(apiFetch).mockImplementation(((url: string) => {
    if (url === "/api/v1/settings/billing-cycle") {
      return Promise.resolve({ billing_cycle_day: 1 });
    }
    if (url === "/api/v1/settings/billing-period") {
      return Promise.resolve({ id: 1, start_date: "2026-05-01", end_date: null });
    }
    if (url === "/api/v1/settings") return Promise.resolve([]);
    if (typeof url === "string" && url.startsWith("/api/v1/orgs/members?")) return Promise.resolve({ items: [], total: 0, limit: 25, offset: 0 });
    if (typeof url === "string" && url.startsWith("/api/v1/orgs/invitations?")) return Promise.resolve({ items: [], total: 0, limit: 25, offset: 0 });
    if (url === "/api/v1/category-rules") return Promise.resolve([]);
    return Promise.resolve({});
  }) as never);
}

function mockUser() {
  vi.mocked(useAuth).mockReturnValue({
    user: makeUser() as never,
    loading: false,
    needsSetup: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn().mockResolvedValue(undefined),
  } as never);
}

describe("Billing period polish: inline validation, busy state, error mapping", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    pushMock.mockReset();
    replaceMock.mockReset();
    baseFixtures();
    mockUser();
  });

  it("disables Save when the value matches what the server already has", async () => {
    render(<OrganizationSettingsPage />);
    const input = (await screen.findByLabelText(
      /Billing cycle day/i,
    )) as HTMLInputElement;
    // billing-cycle GET resolves to 1 from baseFixtures.
    await waitFor(() => expect(input.value).toBe("1"));
    const saveBtn = screen.getAllByRole("button", { name: /^Save$/i })[0];
    expect(saveBtn).toBeDisabled();
  });

  it("surfaces inline error and disables Save for out-of-range input", async () => {
    render(<OrganizationSettingsPage />);
    const input = await screen.findByLabelText(/Billing cycle day/i);
    fireEvent.change(input, { target: { value: "31" } });
    const err = await screen.findByRole("alert");
    expect(err.textContent).toMatch(/between 1 and 28/);
    const saveBtn = screen.getAllByRole("button", { name: /^Save$/i })[0];
    expect(saveBtn).toBeDisabled();
  });

  it("clears the inline error once the value becomes valid", async () => {
    render(<OrganizationSettingsPage />);
    const input = await screen.findByLabelText(/Billing cycle day/i);
    fireEvent.change(input, { target: { value: "31" } });
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    fireEvent.change(input, { target: { value: "15" } });
    await waitFor(() =>
      expect(screen.queryByRole("alert")).not.toBeInTheDocument(),
    );
    const saveBtn = screen.getAllByRole("button", { name: /^Save$/i })[0];
    expect(saveBtn).toBeEnabled();
  });

  it("renders the day-rule hint and ties it to the input", async () => {
    render(<OrganizationSettingsPage />);
    const input = await screen.findByLabelText(/Billing cycle day/i);
    const ids = (input.getAttribute("aria-describedby") || "").split(/\s+/);
    expect(ids.some((id) => /hint/.test(id))).toBe(true);
    const hint = document.getElementById(
      ids.find((id) => /hint/.test(id)) || "",
    );
    expect(hint?.textContent).toMatch(/Day of the month/i);
  });

  // ── L5.5 form polish: Cancel + projected close preview ──────────────────
  //
  // Cancel mirrors the Forecast Plans Save/Cancel pattern: it only shows
  // up when the field is dirty and reverts to the last server-confirmed
  // value. The projected-close preview surfaces under the input so the
  // admin sees the consequence of saving before they commit.

  it("hides Cancel when the field is clean and shows it once dirty", async () => {
    render(<OrganizationSettingsPage />);
    const input = (await screen.findByLabelText(
      /Billing cycle day/i,
    )) as HTMLInputElement;
    await waitFor(() => expect(input.value).toBe("1"));
    // Clean state: only the row-level rename Cancel + member Cancel may
    // exist; the billing-cycle Cancel must not.
    expect(
      screen.queryByRole("button", {
        name: /Cancel billing cycle day edit/i,
      }),
    ).not.toBeInTheDocument();

    fireEvent.change(input, { target: { value: "15" } });
    expect(
      await screen.findByRole("button", {
        name: /Cancel billing cycle day edit/i,
      }),
    ).toBeInTheDocument();
  });

  it("Cancel reverts the field to the saved value and clears errors", async () => {
    render(<OrganizationSettingsPage />);
    const input = (await screen.findByLabelText(
      /Billing cycle day/i,
    )) as HTMLInputElement;
    await waitFor(() => expect(input.value).toBe("1"));

    // Dirty + invalid first, to confirm the error clears too.
    fireEvent.change(input, { target: { value: "31" } });
    expect(await screen.findByRole("alert")).toBeInTheDocument();

    const cancel = await screen.findByRole("button", {
      name: /Cancel billing cycle day edit/i,
    });
    fireEvent.click(cancel);

    await waitFor(() => expect(input.value).toBe("1"));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    // Cancel removes itself once the field is clean again.
    expect(
      screen.queryByRole("button", {
        name: /Cancel billing cycle day edit/i,
      }),
    ).not.toBeInTheDocument();
  });

  it("renders the projected close preview when the new value is dirty + valid", async () => {
    render(<OrganizationSettingsPage />);
    const input = (await screen.findByLabelText(
      /Billing cycle day/i,
    )) as HTMLInputElement;
    await waitFor(() => expect(input.value).toBe("1"));

    fireEvent.change(input, { target: { value: "15" } });
    // start_date=2026-05-01, new cycle day=15 → projected close = 2026-06-14
    await waitFor(() => {
      expect(
        screen.getByText(/Saving will close the current period on 2026-06-14/i),
      ).toBeInTheDocument();
    });
  });

  it("clears the preview once the value matches the saved one again", async () => {
    render(<OrganizationSettingsPage />);
    const input = (await screen.findByLabelText(
      /Billing cycle day/i,
    )) as HTMLInputElement;
    await waitFor(() => expect(input.value).toBe("1"));

    fireEvent.change(input, { target: { value: "15" } });
    await waitFor(() =>
      expect(
        screen.getByText(/Saving will close the current period/i),
      ).toBeInTheDocument(),
    );
    fireEvent.change(input, { target: { value: "1" } });
    await waitFor(() => {
      expect(
        screen.queryByText(/Saving will close the current period/i),
      ).not.toBeInTheDocument();
    });
  });

  it("maps a 422 save error to friendly copy without echoing raw body", async () => {
    render(<OrganizationSettingsPage />);
    const input = await screen.findByLabelText(/Billing cycle day/i);
    fireEvent.change(input, { target: { value: "15" } });

    vi.mocked(apiFetch).mockImplementation(((url: string, opts?: RequestInit) => {
      if (url === "/api/v1/settings/billing-cycle" && opts?.method === "PUT") {
        return Promise.reject(
          new ApiResponseError(422, "billing_cycle_day: ensure this value is less than or equal to 28"),
        );
      }
      if (url === "/api/v1/settings/billing-cycle") {
        return Promise.resolve({ billing_cycle_day: 1 });
      }
      if (url === "/api/v1/settings/billing-period") {
        return Promise.resolve({ id: 1, start_date: "2026-05-01", end_date: null });
      }
      return Promise.resolve([]);
    }) as never);

    const saveBtn = screen.getAllByRole("button", { name: /^Save$/i })[0];
    fireEvent.click(saveBtn);

    const pageError = await screen.findByRole("alert");
    expect(pageError.textContent).toMatch(/between 1 and 28/);
    // Raw server detail must not bleed through.
    expect(pageError.textContent).not.toMatch(/ensure this value/i);
    // Value is preserved for retry.
    expect((input as HTMLInputElement).value).toBe("15");
  });
});
