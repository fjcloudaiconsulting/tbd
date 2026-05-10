import { act, render, screen } from "@testing-library/react";

import { useAuth } from "@/components/auth/AuthProvider";

/**
 * AppShell-level CTA route-gating integration tests.
 *
 * These cover the full mount path: AppShell reads usePathname(),
 * passes it to shouldShowAddTransactionCta, and conditionally renders
 * the CTA. Pure-function tests for the predicate live in
 * appshell-add-transaction-cta.test.tsx; this file confirms the
 * predicate is wired into the shell correctly.
 *
 * Each describe block re-mocks usePathname via vi.doMock and a fresh
 * dynamic import of AppShell so the module captures the new pathname
 * at evaluation time.
 */

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

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    apiFetch: vi.fn(async () => [] as never),
  };
});

const BASE_USER = {
  id: 1,
  username: "alice",
  email: "alice@example.com",
  first_name: "Alice",
  last_name: null,
  phone: null,
  avatar_url: null,
  email_verified: true,
  role: "owner",
  org_id: 1,
  org_name: "Acme",
  billing_cycle_day: 1,
  is_superadmin: false,
  is_active: true,
  mfa_enabled: false,
  subscription_status: null,
  subscription_plan: null,
  trial_end: null,
};

function setupAuth() {
  vi.mocked(useAuth).mockReturnValue({
    user: BASE_USER as never,
    loading: false,
    needsSetup: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn(),
  });
}

// Routes where the CTA must render. All seven core money routes from the
// locked design direction.
const SHOW_ROUTES = [
  "/dashboard",
  "/transactions",
  "/accounts",
  "/categories",
  "/forecast-plans",
  "/budgets",
  "/recurring",
];

// Routes that share the AppShell chrome but should NOT carry the brass
// CTA: settings (security/personal), admin (platform), system (plans).
const HIDE_ROUTES = ["/settings/security", "/admin/orgs", "/system/plans"];

for (const route of SHOW_ROUTES) {
  describe(`AppShell renders CTA on ${route}`, () => {
    beforeEach(() => {
      vi.resetModules();
      vi.doMock("next/navigation", () => ({
        useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
        usePathname: () => route,
      }));
    });

    afterEach(() => {
      vi.doUnmock("next/navigation");
    });

    it("mounts the brass + New Transaction CTA in the header", async () => {
      setupAuth();
      const { default: ShellOnRoute } = await import("@/components/AppShell");
      await act(async () => {
        render(
          <ShellOnRoute>
            <p>page body</p>
          </ShellOnRoute>,
        );
      });
      expect(screen.getByTestId("appshell-add-transaction-cta")).toBeInTheDocument();
    });
  });
}

for (const route of HIDE_ROUTES) {
  describe(`AppShell hides CTA on ${route}`, () => {
    beforeEach(() => {
      vi.resetModules();
      vi.doMock("next/navigation", () => ({
        useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
        usePathname: () => route,
      }));
    });

    afterEach(() => {
      vi.doUnmock("next/navigation");
    });

    it("does not mount the CTA in the header", async () => {
      setupAuth();
      const { default: ShellOnRoute } = await import("@/components/AppShell");
      await act(async () => {
        render(
          <ShellOnRoute>
            <p>page body</p>
          </ShellOnRoute>,
        );
      });
      expect(screen.queryByTestId("appshell-add-transaction-cta")).toBeNull();
    });
  });
}
