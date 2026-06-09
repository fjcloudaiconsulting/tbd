import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import SystemPlansPage from "@/app/system/plans/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";

const replaceMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: replaceMock }),
  usePathname: () => "/system/plans",
}));

vi.mock("@/components/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app-shell">{children}</div>
  ),
}));

vi.mock("@/components/auth/AuthProvider", () => ({
  useAuth: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

const SUPERADMIN = {
  id: 1, username: "root", email: "root@platform.io",
  first_name: null, last_name: null, phone: null, avatar_url: null,
  email_verified: true, role: "owner" as const, org_id: 1, org_name: "Platform",
  billing_cycle_day: 1, is_superadmin: true, is_active: true,
  mfa_enabled: false, subscription_status: null, subscription_plan: null,
  trial_end: null,
};

const NON_SUPERADMIN_BASE = { ...SUPERADMIN, id: 2, is_superadmin: false };
const NON_SUPERADMIN_NO_PERMS = { ...NON_SUPERADMIN_BASE };
const NON_SUPERADMIN_WITH_PLANS = {
  ...NON_SUPERADMIN_BASE,
  id: 3,
  permissions: ["plans.manage"],
};

const PRO_PLAN = {
  id: 1,
  name: "Pro",
  slug: "pro",
  description: "",
  is_custom: false,
  is_active: true,
  sort_order: 1,
  price_monthly: 10,
  price_yearly: 100,
  max_users: null,
  retention_days: null,
  features: {
    "ai.budget": true,
    "ai.forecast": false,
    "ai.smart_plan": false,
    "ai.autocategorize": false,
  },
};

// /api/v1/plans/all now returns a ListEnvelope; the table sends sort +
// pagination query params, so match on the path prefix not an exact URL.
const isPlansAll = (url: unknown): url is string =>
  typeof url === "string" && url.startsWith("/api/v1/plans/all");
const plansEnvelope = (items: unknown[]) =>
  Promise.resolve({ items, total: items.length, limit: 25, offset: 0 });

describe("/system/plans page — Features section + Duplicate", () => {
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
    } as never);
  });

  it("renders one Features row per catalog key in the editor", async () => {
    apiFetchMock.mockImplementation(((url: string) => {
      if (isPlansAll(url)) return plansEnvelope([PRO_PLAN]);
      return Promise.resolve(undefined);
    }) as never);

    render(<SystemPlansPage />);

    // Wait for the row to render before opening the editor.
    const editButton = await screen.findByRole("button", { name: /^Edit$/i });
    fireEvent.click(editButton);

    // The four FEATURE_LABELS labels should each appear in the editor.
    await screen.findByText("AI Budget Rebalancing");
    expect(screen.getByText("AI Smart Forecast")).toBeInTheDocument();
    expect(screen.getByText("AI Goal-Based Plans")).toBeInTheDocument();
    expect(screen.getByText("AI Auto-Categorization")).toBeInTheDocument();
  });

  it("Edit Save sends features without slug", async () => {
    apiFetchMock.mockImplementation(((url: string) => {
      if (isPlansAll(url)) return plansEnvelope([PRO_PLAN]);
      return Promise.resolve(undefined);
    }) as never);

    render(<SystemPlansPage />);

    const editButton = await screen.findByRole("button", { name: /^Edit$/i });
    fireEvent.click(editButton);

    // Wait for the editor to render the feature checkboxes.
    await screen.findByText("AI Auto-Categorization");

    // Toggle ai.autocategorize. The checkbox has id="feature-ai.autocategorize".
    const autocatCheckbox = document.getElementById("feature-ai.autocategorize") as HTMLInputElement;
    expect(autocatCheckbox).toBeTruthy();
    fireEvent.click(autocatCheckbox);

    // Click the form's Save button.
    fireEvent.click(screen.getByRole("button", { name: /^Save$/i }));

    await waitFor(() => {
      const editCall = apiFetchMock.mock.calls.find(
        ([url, opts]) =>
          typeof url === "string" &&
          url.includes("/api/v1/plans/1") &&
          (opts as RequestInit | undefined)?.method === "PUT",
      );
      expect(editCall).toBeDefined();
      const body = JSON.parse((editCall![1] as RequestInit).body as string);
      expect(body).not.toHaveProperty("slug");
      expect(body).toHaveProperty("features");
      expect(body.features["ai.autocategorize"]).toBe(true);
    });
  });

  it("redirects non-superadmin without plans.manage to /dashboard", async () => {
    useAuthMock.mockReturnValue({
      user: NON_SUPERADMIN_NO_PERMS as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);

    const { container } = render(<SystemPlansPage />);
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/dashboard"));
    // Component returns null while gated.
    expect(container).toBeEmptyDOMElement();
    // /api/v1/plans/all must NOT be called when the user is gated out.
    const planFetches = apiFetchMock.mock.calls.filter(
      ([url]) => isPlansAll(url),
    );
    expect(planFetches).toHaveLength(0);
  });

  it("redirects unauthenticated visitors to /login (auth settled, user=null)", async () => {
    useAuthMock.mockReturnValue({
      user: null as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);

    const { container } = render(<SystemPlansPage />);
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/login"));
    expect(container).toBeEmptyDOMElement();
    const planFetches = apiFetchMock.mock.calls.filter(
      ([url]) => isPlansAll(url),
    );
    expect(planFetches).toHaveLength(0);
  });

  it("renders for non-superadmin who carries plans.manage", async () => {
    useAuthMock.mockReturnValue({
      user: NON_SUPERADMIN_WITH_PLANS as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);
    apiFetchMock.mockImplementation(((url: string) => {
      if (isPlansAll(url)) return plansEnvelope([PRO_PLAN]);
      return Promise.resolve(undefined);
    }) as never);

    render(<SystemPlansPage />);

    expect(await screen.findByText("Plan Management")).toBeInTheDocument();
    expect(replaceMock).not.toHaveBeenCalled();
    const planFetches = apiFetchMock.mock.calls.filter(([url]) =>
      isPlansAll(url),
    );
    expect(planFetches.length).toBeGreaterThan(0);
  });

  it("Duplicate row action opens the Duplicate plan modal", async () => {
    apiFetchMock.mockImplementation(((url: string) => {
      if (isPlansAll(url)) return plansEnvelope([PRO_PLAN]);
      return Promise.resolve(undefined);
    }) as never);

    render(<SystemPlansPage />);

    const duplicateButton = await screen.findByRole("button", { name: /^Duplicate$/i });
    fireEvent.click(duplicateButton);

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: /Duplicate plan/i }),
      ).toBeInTheDocument();
    });
  });
});
