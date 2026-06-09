import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import OrganizationSettingsPage from "@/app/settings/organization/page";
import { apiFetch, ApiResponseError } from "@/lib/api";
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
const ORG_ID = 42;

function makeUser(role: "owner" | "admin" | "member") {
  return {
    id: 1, username: "u", email: "u@x.io",
    first_name: null, last_name: null, phone: null, avatar_url: null,
    email_verified: true,
    role,
    org_id: ORG_ID, org_name: ORG_NAME, billing_cycle_day: 1,
    is_superadmin: false, is_active: true, mfa_enabled: false,
    subscription_status: null, subscription_plan: null, trial_end: null,
  };
}

function mockApiBaseFixtures() {
  vi.mocked(apiFetch).mockImplementation(((url: string) => {
    if (url === "/api/v1/settings/billing-cycle") return Promise.resolve({ billing_cycle_day: 1 });
    if (url === "/api/v1/settings/billing-period") return Promise.resolve({ id: 1, start_date: "2026-05-01", end_date: null });
    if (url === "/api/v1/settings") return Promise.resolve([]);
    if (typeof url === "string" && url.startsWith("/api/v1/orgs/members?")) return Promise.resolve({ items: [], total: 0, limit: 25, offset: 0 });
    if (typeof url === "string" && url.startsWith("/api/v1/orgs/invitations?")) return Promise.resolve({ items: [], total: 0, limit: 25, offset: 0 });
    if (url === "/api/v1/category-rules") return Promise.resolve([]);
    return Promise.resolve({});
  }) as never);
}

function mockUser(role: "owner" | "admin" | "member", refreshMe = vi.fn()) {
  vi.mocked(useAuth).mockReturnValue({
    user: makeUser(role) as never,
    loading: false,
    needsSetup: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe,
  } as never);
  return refreshMe;
}

describe("OrganizationSettingsPage — rename", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    pushMock.mockReset();
    replaceMock.mockReset();
    vi.mocked(mutate).mockClear();
    mockApiBaseFixtures();
  });

  it("renames org successfully and refreshes user", async () => {
    const refreshMe = vi.fn().mockResolvedValue(undefined);
    mockUser("owner", refreshMe);

    // Layer the rename success on top of the base GET fixtures.
    vi.mocked(apiFetch).mockImplementation(((url: string, init?: RequestInit) => {
      if (init?.method === "PATCH" && url === `/api/v1/orgs/${ORG_ID}/rename`) {
        return Promise.resolve({ id: ORG_ID, name: "Acme Inc", billing_cycle_day: 1 });
      }
      if (url === "/api/v1/settings/billing-cycle") return Promise.resolve({ billing_cycle_day: 1 });
      if (url === "/api/v1/settings/billing-period") return Promise.resolve({ id: 1, start_date: "2026-05-01", end_date: null });
      if (url === "/api/v1/settings") return Promise.resolve([]);
      if (typeof url === "string" && url.startsWith("/api/v1/orgs/members?")) return Promise.resolve({ items: [], total: 0, limit: 25, offset: 0 });
      if (typeof url === "string" && url.startsWith("/api/v1/orgs/invitations?")) return Promise.resolve({ items: [], total: 0, limit: 25, offset: 0 });
      if (url === "/api/v1/category-rules") return Promise.resolve([]);
      return Promise.resolve({});
    }) as never);

    render(<OrganizationSettingsPage />);

    // Open the edit form.
    const renameButton = await screen.findByRole("button", { name: /^rename$/i });
    fireEvent.click(renameButton);

    const input = screen.getByLabelText(/new organization name/i) as HTMLInputElement;
    expect(input.value).toBe(ORG_NAME);

    // Type a new name and submit.
    fireEvent.change(input, { target: { value: "Acme Inc" } });
    fireEvent.click(screen.getByRole("button", { name: /save organization name/i }));

    await waitFor(() => {
      const call = vi.mocked(apiFetch).mock.calls.find(
        ([url, opts]) => url === `/api/v1/orgs/${ORG_ID}/rename` && (opts as RequestInit | undefined)?.method === "PATCH",
      );
      expect(call).toBeTruthy();
      const init = call![1] as RequestInit;
      expect(JSON.parse(init.body as string)).toEqual({ name: "Acme Inc" });
    });

    await waitFor(() => expect(refreshMe).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.getByText(/organization renamed/i)).toBeInTheDocument(),
    );
  });

  it("shows 409 error message on duplicate", async () => {
    mockUser("owner");

    vi.mocked(apiFetch).mockImplementation(((url: string, init?: RequestInit) => {
      if (init?.method === "PATCH" && url === `/api/v1/orgs/${ORG_ID}/rename`) {
        return Promise.reject(
          new ApiResponseError(409, "An organization with that name already exists"),
        );
      }
      if (url === "/api/v1/settings/billing-cycle") return Promise.resolve({ billing_cycle_day: 1 });
      if (url === "/api/v1/settings/billing-period") return Promise.resolve({ id: 1, start_date: "2026-05-01", end_date: null });
      if (url === "/api/v1/settings") return Promise.resolve([]);
      if (typeof url === "string" && url.startsWith("/api/v1/orgs/members?")) return Promise.resolve({ items: [], total: 0, limit: 25, offset: 0 });
      if (typeof url === "string" && url.startsWith("/api/v1/orgs/invitations?")) return Promise.resolve({ items: [], total: 0, limit: 25, offset: 0 });
      if (url === "/api/v1/category-rules") return Promise.resolve([]);
      return Promise.resolve({});
    }) as never);

    render(<OrganizationSettingsPage />);

    fireEvent.click(await screen.findByRole("button", { name: /^rename$/i }));
    const input = screen.getByLabelText(/new organization name/i);
    fireEvent.change(input, { target: { value: "Other Co" } });
    fireEvent.click(screen.getByRole("button", { name: /save organization name/i }));

    await waitFor(() =>
      expect(
        screen.getByText(/an organization with that name already exists/i),
      ).toBeInTheDocument(),
    );
    // Form stays open so the user can correct.
    expect(screen.getByLabelText(/new organization name/i)).toBeInTheDocument();
  });

  it("non-owner sees read-only display, no form", async () => {
    mockUser("admin");
    render(<OrganizationSettingsPage />);

    // Org name still visible somewhere on the card.
    await waitFor(() =>
      expect(screen.getAllByText(ORG_NAME).length).toBeGreaterThan(0),
    );
    // No Rename button for non-owners.
    expect(screen.queryByRole("button", { name: /^rename$/i })).toBeNull();
    // No edit input either.
    expect(screen.queryByLabelText(/new organization name/i)).toBeNull();
  });

  it("validates client-side empty input", async () => {
    mockUser("owner");
    render(<OrganizationSettingsPage />);

    fireEvent.click(await screen.findByRole("button", { name: /^rename$/i }));
    const input = screen.getByLabelText(/new organization name/i);

    // Whitespace-only input → Save button is disabled (no API call fires).
    fireEvent.change(input, { target: { value: "   " } });
    const saveButton = screen.getByRole("button", { name: /save organization name/i }) as HTMLButtonElement;
    expect(saveButton.disabled).toBe(true);

    // No PATCH was issued.
    const patchCalls = vi.mocked(apiFetch).mock.calls.filter(
      ([url, opts]) => url === `/api/v1/orgs/${ORG_ID}/rename` && (opts as RequestInit | undefined)?.method === "PATCH",
    );
    expect(patchCalls.length).toBe(0);
  });

  it("cancels edit returns to read-only view without API call", async () => {
    mockUser("owner");
    render(<OrganizationSettingsPage />);

    fireEvent.click(await screen.findByRole("button", { name: /^rename$/i }));

    const input = screen.getByLabelText(/new organization name/i) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "Some Draft" } });
    fireEvent.click(screen.getByRole("button", { name: /cancel organization rename/i }));

    // Form gone, original name still showing.
    await waitFor(() =>
      expect(screen.queryByLabelText(/new organization name/i)).toBeNull(),
    );
    expect(screen.getAllByText(ORG_NAME).length).toBeGreaterThan(0);

    // No PATCH was issued.
    const patchCalls = vi.mocked(apiFetch).mock.calls.filter(
      ([url, opts]) => url === `/api/v1/orgs/${ORG_ID}/rename` && (opts as RequestInit | undefined)?.method === "PATCH",
    );
    expect(patchCalls.length).toBe(0);
  });
});
