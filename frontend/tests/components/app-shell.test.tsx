import { act, render, screen } from "@testing-library/react";

import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";
import { logger } from "@/lib/logger";

vi.mock("@/lib/logger", () => ({
  logger: {
    debug: vi.fn(),
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
  },
}));

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

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/dashboard",
}));

// AppShellAddTransactionCta loads accounts/categories on mount; stub the
// fetch so these system-nav-focused tests don't trip the act() warning
// when the CTA's loadRefs settles after assertions complete.
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

async function renderShell() {
  // The AppShell-level CTA fires apiFetch in a useEffect. Wrap render
  // in act() so the resulting state updates flush before assertions,
  // skipping this trips the React act() warning in the existing
  // synchronous tests.
  await act(async () => {
    render(
      <AppShell>
        <p>page body</p>
      </AppShell>,
    );
  });
}

describe("AppShell — system nav gating", () => {
  const useAuthMock = vi.mocked(useAuth);

  beforeEach(() => {
    useAuthMock.mockReset();
  });

  it("hides the System nav for a regular user without admin.view", async () => {
    useAuthMock.mockReturnValue({
      user: BASE_USER as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    await renderShell();

    expect(screen.queryByText(/^System$/)).toBeNull();
    // Sidebar nav still shows Dashboard for the user.
    expect(screen.getAllByText("Dashboard").length).toBeGreaterThan(0);
  });

  it("shows the System nav for a superadmin (short-circuit)", async () => {
    useAuthMock.mockReturnValue({
      user: { ...BASE_USER, is_superadmin: true } as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    await renderShell();

    expect(screen.getByText(/^System$/)).toBeInTheDocument();
    expect(screen.getByText("Admin")).toBeInTheDocument();
    expect(screen.getByText("Organizations")).toBeInTheDocument();
    expect(screen.getByText("Audit log")).toBeInTheDocument();
  });

  it("shows the System nav for a non-superadmin who carries admin.view in permissions", async () => {
    useAuthMock.mockReturnValue({
      user: { ...BASE_USER, permissions: ["admin.view"] } as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    await renderShell();

    expect(screen.getByText(/^System$/)).toBeInTheDocument();
    expect(screen.getByText("Admin")).toBeInTheDocument();
    // admin.view alone does NOT grant the more specific destinations.
    expect(screen.queryByText("Organizations")).toBeNull();
    expect(screen.queryByText("Audit log")).toBeNull();
    expect(screen.queryByText("Plans")).toBeNull();
  });

  it("shows only the Audit log link for a non-superadmin with audit.view alone", async () => {
    useAuthMock.mockReturnValue({
      user: { ...BASE_USER, permissions: ["audit.view"] } as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    await renderShell();

    expect(screen.getByText(/^System$/)).toBeInTheDocument();
    expect(screen.getByText("Audit log")).toBeInTheDocument();
    expect(screen.queryByText("Admin")).toBeNull();
    expect(screen.queryByText("Organizations")).toBeNull();
    expect(screen.queryByText("Plans")).toBeNull();
  });

  it("shows only the Organizations link for a non-superadmin with orgs.view alone", async () => {
    useAuthMock.mockReturnValue({
      user: { ...BASE_USER, permissions: ["orgs.view"] } as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    await renderShell();

    expect(screen.getByText(/^System$/)).toBeInTheDocument();
    expect(screen.getByText("Organizations")).toBeInTheDocument();
    expect(screen.queryByText("Admin")).toBeNull();
    expect(screen.queryByText("Audit log")).toBeNull();
    expect(screen.queryByText("Plans")).toBeNull();
  });

  it("shows only the Plans link for a non-superadmin with plans.manage alone", async () => {
    useAuthMock.mockReturnValue({
      user: { ...BASE_USER, permissions: ["plans.manage"] } as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    await renderShell();

    expect(screen.getByText(/^System$/)).toBeInTheDocument();
    expect(screen.getByText("Plans")).toBeInTheDocument();
    expect(screen.queryByText("Admin")).toBeNull();
    expect(screen.queryByText("Organizations")).toBeNull();
    expect(screen.queryByText("Audit log")).toBeNull();
  });
});

// ── 2026-05-18 idle-recovery observability ──────────────────────────────
//
// apiFetch dispatches ``auth:refresh-attempt`` and
// ``auth:retry-after-refresh`` CustomEvents on every silent-refresh
// outcome. AppShell subscribes and pipes them into ``@/lib/logger``,
// which in the browser writes to ``console.*`` only — App Platform's
// log shipper captures backend stdout/stderr, NOT browser console,
// so these events DO NOT reach production logs yet. The subscription
// is kept as the hook point for a follow-up client-telemetry sink.
// These tests pin the subscription's contract (info on ok / 2xx,
// warn on transient/terminal/non-2xx) so the wiring is ready when
// the sink lands.

describe("AppShell — auth refresh observability", () => {
  const useAuthMock = vi.mocked(useAuth);
  const loggerInfo = vi.mocked(logger.info);
  const loggerWarn = vi.mocked(logger.warn);

  beforeEach(() => {
    useAuthMock.mockReset();
    loggerInfo.mockReset();
    loggerWarn.mockReset();
    useAuthMock.mockReturnValue({
      user: BASE_USER as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });
  });

  it("logs auth:refresh-attempt with attempt + outcome + duration", async () => {
    await renderShell();

    act(() => {
      window.dispatchEvent(
        new CustomEvent("auth:refresh-attempt", {
          detail: { attempt: 1, outcome: "ok", durationMs: 28_412 },
        }),
      );
    });

    expect(loggerInfo).toHaveBeenCalledWith("auth.refresh-attempt", {
      attempt: 1,
      outcome: "ok",
      status: undefined,
      duration_ms: 28_412,
    });
  });

  it("logs auth:refresh-attempt as warn when outcome is transient", async () => {
    await renderShell();

    act(() => {
      window.dispatchEvent(
        new CustomEvent("auth:refresh-attempt", {
          detail: { attempt: 1, outcome: "transient", durationMs: 45_001 },
        }),
      );
    });

    expect(loggerWarn).toHaveBeenCalledWith("auth.refresh-attempt", {
      attempt: 1,
      outcome: "transient",
      status: undefined,
      duration_ms: 45_001,
    });
  });

  it("logs auth:refresh-attempt as warn when outcome is terminal (401/403)", async () => {
    await renderShell();

    act(() => {
      window.dispatchEvent(
        new CustomEvent("auth:refresh-attempt", {
          detail: { attempt: 1, outcome: "terminal", status: 401, durationMs: 120 },
        }),
      );
    });

    expect(loggerWarn).toHaveBeenCalledWith("auth.refresh-attempt", {
      attempt: 1,
      outcome: "terminal",
      status: 401,
      duration_ms: 120,
    });
  });

  it("logs auth:retry-after-refresh with path + status + ok", async () => {
    await renderShell();

    act(() => {
      window.dispatchEvent(
        new CustomEvent("auth:retry-after-refresh", {
          detail: {
            path: "/api/v1/accounts",
            status: 200,
            ok: true,
            durationMs: 87,
          },
        }),
      );
    });

    expect(loggerInfo).toHaveBeenCalledWith("auth.retry-after-refresh", {
      path: "/api/v1/accounts",
      status: 200,
      ok: true,
      duration_ms: 87,
    });
  });

  it("logs auth:retry-after-refresh as warn when retry was non-2xx", async () => {
    await renderShell();

    act(() => {
      window.dispatchEvent(
        new CustomEvent("auth:retry-after-refresh", {
          detail: {
            path: "/api/v1/admin/orgs",
            status: 403,
            ok: false,
            durationMs: 95,
          },
        }),
      );
    });

    expect(loggerWarn).toHaveBeenCalledWith("auth.retry-after-refresh", {
      path: "/api/v1/admin/orgs",
      status: 403,
      ok: false,
      duration_ms: 95,
    });
  });
});
