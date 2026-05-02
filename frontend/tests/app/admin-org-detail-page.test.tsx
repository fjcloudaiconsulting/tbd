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

const FEATURE_STATE = {
  plan: { id: 1, name: "Free", slug: "free" },
  features: [
    { key: "ai.budget", plan_default: true, effective: true, override: null },
    { key: "ai.forecast", plan_default: false, effective: false, override: null },
    { key: "ai.smart_plan", plan_default: false, effective: false, override: null },
    { key: "ai.autocategorize", plan_default: false, effective: false, override: null },
  ],
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
    apiFetchMock.mockImplementation(((url: string) => {
      if (url === `/api/v1/admin/orgs/42/feature-state`) return Promise.resolve(FEATURE_STATE);
      if (url.startsWith("/api/v1/admin/orgs/42")) return Promise.resolve(DETAIL);
      if (url === "/api/v1/plans") return Promise.resolve([{ id: 1, slug: "free", name: "Free" }]);
      return Promise.resolve(undefined);
    }) as never);
    render(<AdminOrgDetailPage />);

    const deleteBtn = await screen.findByRole("button", { name: /Delete organization/i });
    expect(deleteBtn).toBeDisabled();

    const confirmInput = screen.getByLabelText(/Confirm organization name/i);
    fireEvent.change(confirmInput, { target: { value: "acme" } }); // wrong case
    expect(deleteBtn).toBeDisabled();

    fireEvent.change(confirmInput, { target: { value: "Acme" } });
    expect(deleteBtn).not.toBeDisabled();
  });

  it("posts current_period_end when admin changes the date and saves", async () => {
    apiFetchMock.mockImplementation(((url: string, opts?: RequestInit) => {
      if (url.startsWith("/api/v1/admin/orgs/42/subscription") && opts?.method === "PUT") {
        return Promise.resolve({ before: {}, after: {} });
      }
      if (url === `/api/v1/admin/orgs/42/feature-state`) return Promise.resolve(FEATURE_STATE);
      if (url.startsWith("/api/v1/admin/orgs/42")) return Promise.resolve(DETAIL);
      if (url === "/api/v1/plans") {
        return Promise.resolve([
          { id: 1, slug: "free", name: "Free" },
          { id: 2, slug: "pro", name: "Pro" },
        ]);
      }
      return Promise.resolve(undefined);
    }) as never);
    render(<AdminOrgDetailPage />);

    await screen.findByRole("heading", { name: "Acme" });
    fireEvent.change(screen.getByLabelText(/Period end/i), {
      target: { value: "2026-12-31" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/i }));

    await waitFor(() => {
      expect(apiFetchMock).toHaveBeenCalledWith(
        "/api/v1/admin/orgs/42/subscription",
        expect.objectContaining({
          method: "PUT",
          body: JSON.stringify({
            current_period_end: "2026-12-31",
          }),
        }),
      );
    });
  });

  it("Change plan modal posts plan_id to subscription endpoint", async () => {
    apiFetchMock.mockImplementation(((url: string, opts?: RequestInit) => {
      if (url.startsWith("/api/v1/admin/orgs/42/subscription") && opts?.method === "PUT") {
        return Promise.resolve({ before: {}, after: {} });
      }
      if (url === `/api/v1/admin/orgs/42/feature-state`) return Promise.resolve(FEATURE_STATE);
      if (url.startsWith("/api/v1/admin/orgs/42")) return Promise.resolve(DETAIL);
      if (url === "/api/v1/plans") {
        return Promise.resolve([
          { id: 1, slug: "free", name: "Free" },
          { id: 2, slug: "pro", name: "Pro" },
        ]);
      }
      if (url === "/api/v1/plans/all") {
        return Promise.resolve([
          { id: 1, slug: "free", name: "Free" },
          { id: 2, slug: "pro", name: "Pro" },
        ]);
      }
      return Promise.resolve(undefined);
    }) as never);
    render(<AdminOrgDetailPage />);

    await screen.findByRole("heading", { name: "Acme" });
    fireEvent.click(screen.getByRole("button", { name: /Change plan/i }));

    // Modal opens; wait for the modal heading to confirm it mounted, then for
    // the Pro option to load (modal fetches /api/v1/plans/all asynchronously).
    await screen.findByRole("heading", { name: /Change plan/i });
    await screen.findByRole("option", { name: /Pro \(pro\)/i });

    // Pick the Pro plan via the modal's select. The page also has a Status
    // <select>, so grab all comboboxes and pick the modal's (rendered last).
    const comboboxes = screen.getAllByRole("combobox");
    const planSelect = comboboxes[comboboxes.length - 1];
    fireEvent.change(planSelect, { target: { value: "2" } });

    // Submit: there are now two "Save" buttons (the page's + the modal's).
    // The modal's submit button is the last one rendered.
    const saveButtons = screen.getAllByRole("button", { name: /^Save$/i });
    fireEvent.click(saveButtons[saveButtons.length - 1]);

    await waitFor(() => {
      expect(apiFetchMock).toHaveBeenCalledWith(
        "/api/v1/admin/orgs/42/subscription",
        expect.objectContaining({
          method: "PUT",
          body: JSON.stringify({ plan_id: 2 }),
        }),
      );
    });
  });

  it("renders Change plan button", async () => {
    apiFetchMock.mockImplementation(((url: string) => {
      if (url === `/api/v1/admin/orgs/42/feature-state`) return Promise.resolve(FEATURE_STATE);
      if (url.startsWith("/api/v1/admin/orgs/42")) return Promise.resolve(DETAIL);
      if (url === "/api/v1/plans") return Promise.resolve([{ id: 1, slug: "free", name: "Free" }]);
      return Promise.resolve(undefined);
    }) as never);
    render(<AdminOrgDetailPage />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Change plan/i })).toBeInTheDocument();
    });
  });

  it("FeatureOverridesCard renders feature-state rows with override metadata", async () => {
    const featureStateWithOverride = {
      plan: { id: 1, name: "Free", slug: "free" },
      features: [
        { key: "ai.budget", plan_default: true, effective: true, override: null },
        {
          key: "ai.forecast",
          plan_default: false,
          effective: true,
          override: {
            feature_key: "ai.forecast",
            value: true,
            set_by: 1,
            set_by_email: "root@platform.io",
            set_at: "2026-05-01T10:00:00Z",
            expires_at: null,
            note: "manual grant",
            is_expired: false,
          },
        },
        { key: "ai.smart_plan", plan_default: false, effective: false, override: null },
        { key: "ai.autocategorize", plan_default: false, effective: false, override: null },
      ],
    };
    apiFetchMock.mockImplementation(((url: string) => {
      if (url === `/api/v1/admin/orgs/42/feature-state`) return Promise.resolve(featureStateWithOverride);
      if (url.startsWith("/api/v1/admin/orgs/42")) return Promise.resolve(DETAIL);
      if (url === "/api/v1/plans") return Promise.resolve([{ id: 1, slug: "free", name: "Free" }]);
      return Promise.resolve(undefined);
    }) as never);
    render(<AdminOrgDetailPage />);

    await waitFor(() => {
      expect(screen.getByText(/AI Budget Rebalancing/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/AI Smart Forecast/i)).toBeInTheDocument();
    // Override metadata for the row that has set_by_email.
    expect(screen.getByText(/set by root@platform\.io/i)).toBeInTheDocument();
  });

  it("posts the confirm_name and routes back to /admin/orgs after delete", async () => {
    apiFetchMock.mockImplementation(((url: string, opts?: RequestInit) => {
      if (url === `/api/v1/admin/orgs/42/feature-state`) return Promise.resolve(FEATURE_STATE);
      if (url.startsWith("/api/v1/admin/orgs/42") && (!opts || opts.method !== "DELETE")) {
        return Promise.resolve(DETAIL);
      }
      if (url === "/api/v1/plans") return Promise.resolve([{ id: 1, slug: "free", name: "Free" }]);
      if (url === "/api/v1/admin/orgs/42" && opts?.method === "DELETE") {
        return Promise.resolve({ deleted: { organizations: 1 } });
      }
      return Promise.resolve(undefined);
    }) as never);
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
