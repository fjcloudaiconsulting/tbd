import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import AdminOrgDetailPage from "@/app/admin/orgs/[id]/page";
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
  return { ...actual, useAuth: vi.fn(), AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</> };
});

const pushMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, replace: vi.fn() }),
  useParams: () => ({ id: "42" }),
  usePathname: () => "/admin/orgs/42",
}));


const SUPERADMIN = {
  id: 1, username: "root", email: "root@platform.io",
  first_name: null, last_name: null, phone: null, avatar_url: null,
  email_verified: true, role: "owner", org_id: 1, org_name: "Platform",
  billing_cycle_day: 1, is_superadmin: true, is_active: true,
  mfa_enabled: false, subscription_status: null, subscription_plan: null,
  trial_end: null,
};

const DETAIL = {
  id: 42, name: "Acme", billing_cycle_day: 1,
  created_at: "2026-04-15T10:00:00",
  subscription: {
    status: "trialing", plan_id: 1, plan_slug: "free",
    trial_start: "2026-04-15", trial_end: "2026-05-15",
    current_period_start: null, current_period_end: null,
    created_at: "2026-04-15T10:00:00", updated_at: "2026-04-15T10:00:00",
  },
  members: [
    { id: 9, username: "owner", email: "o@a.io", role: "owner", is_active: true, email_verified: true, created_at: null },
  ],
  counts: { transactions: 5, accounts: 1, budgets: 0, forecast_plans: 0 },
};


describe("AdminOrgDetailPage — Danger zone gating", () => {
  const apiFetchMock = vi.mocked(apiFetch);
  const useAuthMock = vi.mocked(useAuth);

  beforeEach(() => {
    apiFetchMock.mockReset();
    pushMock.mockReset();
    useAuthMock.mockReturnValue({
      user: SUPERADMIN as never,
      loading: false, needsSetup: false,
      login: vi.fn(), register: vi.fn(), logout: vi.fn(), refreshMe: vi.fn(),
    });
  });

  it("disables the delete button until the org name is typed exactly", async () => {
    apiFetchMock.mockResolvedValueOnce(DETAIL as never);
    render(<AdminOrgDetailPage />);

    const deleteBtn = await screen.findByRole("button", { name: /Delete organization/i });
    expect(deleteBtn).toBeDisabled();

    const confirmInput = screen.getByLabelText(/Confirm organization name/i);
    fireEvent.change(confirmInput, { target: { value: "acme" } }); // wrong case
    expect(deleteBtn).toBeDisabled();

    fireEvent.change(confirmInput, { target: { value: "Acme" } });
    expect(deleteBtn).not.toBeDisabled();
  });

  it("posts the confirm_name and routes back to /admin/orgs after delete", async () => {
    apiFetchMock
      .mockResolvedValueOnce(DETAIL as never)
      .mockResolvedValueOnce({ deleted: { organizations: 1 } } as never);
    render(<AdminOrgDetailPage />);

    await screen.findByRole("heading", { name: "Acme" });
    fireEvent.change(screen.getByLabelText(/Confirm organization name/i), {
      target: { value: "Acme" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Delete organization/i }));

    await waitFor(() => {
      expect(apiFetchMock).toHaveBeenCalledWith(
        "/api/v1/admin/orgs/42",
        expect.objectContaining({
          method: "DELETE",
          body: JSON.stringify({ confirm_name: "Acme" }),
        }),
      );
      expect(pushMock).toHaveBeenCalledWith("/admin/orgs");
    });
  });
});
