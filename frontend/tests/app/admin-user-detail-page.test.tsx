import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import AdminUserDetailPage from "@/app/admin/users/[user_id]/page";
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

const replaceMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: replaceMock }),
  usePathname: () => "/admin/users/42",
  useParams: () => ({ user_id: "42" }),
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
};

const SAMPLE_DETAIL = {
  id: 42,
  email: "ada@acme.io",
  username: "ada",
  display_name: "Ada Lovelace",
  is_superadmin: false,
  is_active: true,
  email_verified: true,
  mfa_enabled: true,
  password_set: true,
  password_changed_at: "2026-04-30T10:00:00",
  sessions_invalidated_at: null,
  onboarded_at: "2026-04-15T10:00:00",
  created_at: "2026-04-15T10:00:00",
  phone: null,
  orgs: [{ org_id: 10, name: "Acme Co", role: "owner" }],
  recent_audit_events: [
    {
      id: 1,
      event_type: "admin.org.subscription.override",
      outcome: "success",
      target_org_id: 11,
      target_org_name: "Beta",
      created_at: "2026-05-12T15:00:00",
    },
  ],
};

describe("AdminUserDetailPage", () => {
  const apiFetchMock = vi.mocked(apiFetch);
  const useAuthMock = vi.mocked(useAuth);

  beforeEach(() => {
    apiFetchMock.mockReset();
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

  it("renders the user identity card and org membership", async () => {
    apiFetchMock.mockResolvedValueOnce(SAMPLE_DETAIL as never);

    render(<AdminUserDetailPage />);

    // Page title is the display name.
    await screen.findByRole("heading", { name: "Ada Lovelace" });
    // Identity fields present. Email may also appear in the danger-zone
    // warning paragraph, so allow more than one match.
    expect(screen.getAllByText("ada@acme.io").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("ada")).toBeInTheDocument();
    // Org membership link.
    expect(screen.getByRole("link", { name: "Acme Co" })).toHaveAttribute(
      "href",
      "/admin/orgs/10",
    );
    // Recent audit event row.
    expect(screen.getByText("admin.org.subscription.override")).toBeInTheDocument();
  });

  it("redirects non-superadmin users without users.view away from the page", async () => {
    useAuthMock.mockReturnValue({
      user: { ...SUPERADMIN, is_superadmin: false } as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    render(<AdminUserDetailPage />);

    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/dashboard");
    });
  });

  it("shows an error banner on failed fetch", async () => {
    apiFetchMock.mockRejectedValueOnce(new Error("boom"));

    render(<AdminUserDetailPage />);

    await screen.findByRole("alert");
  });

  // ── Delete user (system-level) ─────────────────────────────────

  it("disables the Delete user button when the target is still active", async () => {
    apiFetchMock.mockResolvedValueOnce(SAMPLE_DETAIL as never);

    render(<AdminUserDetailPage />);

    await screen.findByRole("heading", { name: "Ada Lovelace" });
    const deleteBtn = await screen.findByRole("button", {
      name: /delete user ada@acme.io/i,
    });
    expect(deleteBtn).toBeDisabled();
    // The explanatory tooltip text is rendered both as a `title`
    // attribute on the button and as a small text line below.
    expect(deleteBtn).toHaveAttribute(
      "title",
      expect.stringMatching(/deactivate the user first/i),
    );
  });

  it("disables the Delete user button when the target is a superadmin", async () => {
    apiFetchMock.mockResolvedValueOnce({
      ...SAMPLE_DETAIL,
      is_active: false,
      is_superadmin: true,
    } as never);

    render(<AdminUserDetailPage />);

    const deleteBtn = await screen.findByRole("button", {
      name: /delete user ada@acme.io/i,
    });
    expect(deleteBtn).toBeDisabled();
    expect(deleteBtn).toHaveAttribute(
      "title",
      expect.stringMatching(/superadmin/i),
    );
  });

  it("disables the Delete user button when target is the current user", async () => {
    apiFetchMock.mockResolvedValueOnce({
      ...SAMPLE_DETAIL,
      id: SUPERADMIN.id,
      is_active: false,
    } as never);

    render(<AdminUserDetailPage />);

    const deleteBtn = await screen.findByRole("button", {
      name: /delete user ada@acme.io/i,
    });
    expect(deleteBtn).toBeDisabled();
    expect(deleteBtn).toHaveAttribute(
      "title",
      expect.stringMatching(/your own user/i),
    );
  });

  it("hides the danger zone when the actor lacks users.delete", async () => {
    useAuthMock.mockReturnValue({
      user: {
        ...SUPERADMIN,
        is_superadmin: false,
        permissions: ["users.view"],
      } as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });
    apiFetchMock.mockResolvedValueOnce({
      ...SAMPLE_DETAIL,
      is_active: false,
    } as never);

    render(<AdminUserDetailPage />);

    await screen.findByRole("heading", { name: "Ada Lovelace" });
    expect(screen.queryByTestId("user-danger-zone")).not.toBeInTheDocument();
  });

  it("DELETEs the user and navigates back to the list on confirm", async () => {
    // First call: detail fetch. Second call: DELETE.
    apiFetchMock
      .mockResolvedValueOnce({
        ...SAMPLE_DETAIL,
        is_active: false,
      } as never)
      .mockResolvedValueOnce({ deleted_user_id: 42 } as never);

    render(<AdminUserDetailPage />);

    const deleteBtn = await screen.findByRole("button", {
      name: /delete user ada@acme.io/i,
    });
    expect(deleteBtn).not.toBeDisabled();
    fireEvent.click(deleteBtn);

    // ConfirmModal opens; confirm button inside the dialog has the
    // "Delete user" label. Scope the query to the dialog so we don't
    // collide with the danger-zone button that triggered it.
    const dialog = await screen.findByRole("dialog");
    const confirmBtn = within(dialog).getByRole("button", {
      name: /^delete user$/i,
    });
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      expect(apiFetchMock).toHaveBeenCalledWith(
        "/api/v1/admin/users/42",
        expect.objectContaining({ method: "DELETE" }),
      );
    });
    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/admin/users");
    });
  });

  it("closes the modal and surfaces the error when DELETE fails (architect feedback on PR #303)", async () => {
    // The error banner renders in the danger-zone section of the page
    // body. If the confirm modal stays mounted on top of it after a
    // failure, the operator may not see the message. Pin that on a
    // 409 (e.g. someone reactivated the user between page load and
    // confirm), the modal closes and the error banner is visible.
    apiFetchMock
      .mockResolvedValueOnce({
        ...SAMPLE_DETAIL,
        is_active: false,
      } as never)
      .mockRejectedValueOnce(
        Object.assign(new Error("user_still_active"), {
          status: 409,
          payload: { code: "user_still_active", message: "Deactivate first." },
        }) as never,
      );

    render(<AdminUserDetailPage />);

    const deleteBtn = await screen.findByRole("button", {
      name: /delete user ada@acme.io/i,
    });
    fireEvent.click(deleteBtn);

    const dialog = await screen.findByRole("dialog");
    const confirmBtn = within(dialog).getByRole("button", {
      name: /^delete user$/i,
    });
    fireEvent.click(confirmBtn);

    // Modal closes on failure (operator can now see the banner).
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });

    // Banner is rendered with the failure message.
    expect(
      await screen.findByRole("alert"),
    ).toHaveTextContent(/deactivate first|delete failed|user_still_active/i);

    // We did NOT navigate away on failure.
    expect(replaceMock).not.toHaveBeenCalledWith("/admin/users");
  });
});
