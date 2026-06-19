import { act, render, screen } from "@testing-library/react";

import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";
import { ensureFreshAccessToken } from "@/lib/api";
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
//
// Also stub ensureFreshAccessToken so the proactive-refresh focus /
// visibility tests can spy on the call without exercising the real
// JWT-decode + singleflight machinery (that path is covered by
// tests/api/proactive-refresh.test.ts). The keep-real-types pattern
// preserves the named-export shape so AppShell's import succeeds.
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    apiFetch: vi.fn(async () => [] as never),
    ensureFreshAccessToken: vi.fn(async () => undefined),
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
      billingUiEnabled: false,
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
      billingUiEnabled: false,
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
      billingUiEnabled: false,
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
    // System Plan Catalog link gated on plans.manage. Customer "Plans"
    // link in the main nav is always visible — disambiguate by exact
    // accessible name on the system-side label.
    expect(screen.queryByRole("link", { name: "Plan Catalog" })).toBeNull();
  });

  it("shows only the Audit log link for a non-superadmin with audit.view alone", async () => {
    useAuthMock.mockReturnValue({
      user: { ...BASE_USER, permissions: ["audit.view"] } as never,
      loading: false,
      needsSetup: false,
      billingUiEnabled: false,
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
    expect(screen.queryByRole("link", { name: "Plan Catalog" })).toBeNull();
  });

  it("shows only the Organizations link for a non-superadmin with orgs.view alone", async () => {
    useAuthMock.mockReturnValue({
      user: { ...BASE_USER, permissions: ["orgs.view"] } as never,
      loading: false,
      needsSetup: false,
      billingUiEnabled: false,
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
    expect(screen.queryByRole("link", { name: "Plan Catalog" })).toBeNull();
  });

  it("hides the Reports nav entry when features.reports is false", async () => {
    useAuthMock.mockReturnValue({
      user: BASE_USER as never,
      loading: false,
      needsSetup: false,
      features: { reports: false, plans: true },
      billingUiEnabled: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    await renderShell();

    // "Reports" should not appear in the sidebar when the flag is off.
    // Use exact-match so neighboring labels (e.g. "Forecast Plans",
    // "Plans") don't accidentally satisfy the assertion.
    expect(screen.queryByRole("link", { name: "Reports" })).toBeNull();
    // Sanity: the rest of the nav still renders.
    expect(screen.getAllByText("Dashboard").length).toBeGreaterThan(0);
  });

  it("shows the Reports nav entry when features.reports is true", async () => {
    useAuthMock.mockReturnValue({
      user: BASE_USER as never,
      loading: false,
      needsSetup: false,
      features: { reports: true, plans: true },
      billingUiEnabled: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    await renderShell();

    // Exact-match by accessible role so neither "Forecast Plans" nor
    // the existing "Plans" frame item satisfies a substring query.
    expect(
      screen.getByRole("link", { name: "Reports" }),
    ).toBeInTheDocument();
    // Plans entry must remain (architect-locked: Reports is a peer of
    // Plans, not a replacement).
    expect(screen.getByRole("link", { name: "Plans" })).toBeInTheDocument();
  });

  it("hides the Plans nav entry when features.plans is false", async () => {
    useAuthMock.mockReturnValue({
      user: BASE_USER as never,
      loading: false,
      needsSetup: false,
      features: { reports: false, plans: false },
      billingUiEnabled: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    await renderShell();

    // The "/plans" (Plans) item must be absent. Use exact role/name so we
    // don't accidentally match "/forecast-plans" (Forecast Plans) or the
    // system "/system/plans" (Plan Catalog).
    expect(screen.queryByRole("link", { name: "Plans" })).toBeNull();
    // Forecast Plans is a DIFFERENT feature — must remain untouched.
    expect(
      screen.getByRole("link", { name: "Forecast Plans" }),
    ).toBeInTheDocument();
  });

  it("shows the Plans nav entry when features.plans is true", async () => {
    useAuthMock.mockReturnValue({
      user: BASE_USER as never,
      loading: false,
      needsSetup: false,
      features: { reports: false, plans: true },
      billingUiEnabled: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    await renderShell();

    expect(screen.getByRole("link", { name: "Plans" })).toBeInTheDocument();
    // Forecast Plans must be unaffected.
    expect(
      screen.getByRole("link", { name: "Forecast Plans" }),
    ).toBeInTheDocument();
  });

  it("Reports appears after Forecast Plans when Plans is hidden (features.reports=true, features.plans=false)", async () => {
    useAuthMock.mockReturnValue({
      user: BASE_USER as never,
      loading: false,
      needsSetup: false,
      features: { reports: true, plans: false },
      billingUiEnabled: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    await renderShell();

    // Reports still renders when enabled, even without Plans.
    expect(
      screen.getByRole("link", { name: "Reports" }),
    ).toBeInTheDocument();
    // Plans item must be absent.
    expect(screen.queryByRole("link", { name: "Plans" })).toBeNull();
    // Forecast Plans untouched.
    expect(
      screen.getByRole("link", { name: "Forecast Plans" }),
    ).toBeInTheDocument();
  });

  it("shows only the Plan Catalog link for a non-superadmin with plans.manage alone", async () => {
    useAuthMock.mockReturnValue({
      user: { ...BASE_USER, permissions: ["plans.manage"] } as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
      // Gate this test out of the billingUiEnabled hide (PR B 2026-05-29).
      // The test's intent is "plans.manage permission → Plan Catalog visible",
      // not "billing UI hidden by flag" — that's covered separately in
      // tests/appshell-admin-nav-billing-gate.test.tsx.
      billingUiEnabled: true,
    });

    await renderShell();

    expect(screen.getByText(/^System$/)).toBeInTheDocument();
    // System-side subscription-tier catalog renders as "Plan Catalog"
    // to disambiguate from the customer-facing "/plans" scenarios link
    // in the main nav (both would otherwise share the accessible name
    // "Plans" and break ambiguous-selector queries).
    expect(
      screen.getByRole("link", { name: "Plan Catalog" }),
    ).toBeInTheDocument();
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
      billingUiEnabled: false,
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

// ── 2026-05-18 proactive refresh: AppShell focus/visibility wiring ──────
//
// The api.ts proactive-refresh module exposes ensureFreshAccessToken;
// AppShell subscribes to focus + visibilitychange so a backgrounded
// tab returning to the foreground catches up if its setTimeout was
// throttled. These tests pin the AppShell side of that contract:
//
//   - user-gated focus triggers ensureFreshAccessToken
//   - user-gated visibilitychange → visible triggers it
//   - visibilitychange → hidden does NOT trigger (matches the
//     comment "visibilitychange→visible"; without this guard the
//     handler would fire on every transition, doubling traffic
//     and surprising future readers)
//   - no user → no subscription (anonymous landing / login pages
//     never fire proactive refresh)
//   - unmount removes the listeners (no leak across navigations)
describe("AppShell — proactive refresh focus/visibility", () => {
  const useAuthMock = vi.mocked(useAuth);
  const ensureFreshAccessTokenMock = vi.mocked(ensureFreshAccessToken);

  beforeEach(() => {
    useAuthMock.mockReset();
    ensureFreshAccessTokenMock.mockReset();
    ensureFreshAccessTokenMock.mockResolvedValue(undefined);
  });

  function mockSignedInUser() {
    useAuthMock.mockReturnValue({
      user: BASE_USER as never,
      loading: false,
      needsSetup: false,
      billingUiEnabled: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });
  }

  it("focus event fires ensureFreshAccessToken when user is signed in", async () => {
    mockSignedInUser();
    await renderShell();
    ensureFreshAccessTokenMock.mockClear();

    act(() => {
      window.dispatchEvent(new Event("focus"));
    });

    expect(ensureFreshAccessTokenMock).toHaveBeenCalledTimes(1);
  });

  it("visibilitychange → visible fires ensureFreshAccessToken", async () => {
    mockSignedInUser();
    await renderShell();
    ensureFreshAccessTokenMock.mockClear();

    // jsdom doesn't update document.visibilityState on its own;
    // override the getter for this test so the AppShell guard
    // (visibilityState === "visible") evaluates true.
    const visibilityStateSpy = vi
      .spyOn(document, "visibilityState", "get")
      .mockReturnValue("visible");
    try {
      act(() => {
        document.dispatchEvent(new Event("visibilitychange"));
      });
      expect(ensureFreshAccessTokenMock).toHaveBeenCalledTimes(1);
    } finally {
      visibilityStateSpy.mockRestore();
    }
  });

  it("visibilitychange → hidden does NOT fire ensureFreshAccessToken", async () => {
    mockSignedInUser();
    await renderShell();
    ensureFreshAccessTokenMock.mockClear();

    const visibilityStateSpy = vi
      .spyOn(document, "visibilityState", "get")
      .mockReturnValue("hidden");
    try {
      act(() => {
        document.dispatchEvent(new Event("visibilitychange"));
      });
      // Hidden transitions are no-ops — comment intent enforced.
      expect(ensureFreshAccessTokenMock).not.toHaveBeenCalled();
    } finally {
      visibilityStateSpy.mockRestore();
    }
  });

  it("no user: no subscription, focus is a no-op", async () => {
    // user=null + loading=false would trip AppShell's redirect-to-
    // /login useEffect, so set loading=true to keep AppShell
    // rendering the spinner branch without redirect side effects.
    // Either way the proactive-refresh useEffect is gated on `user`
    // and must NOT subscribe when user is falsy.
    useAuthMock.mockReturnValue({
      user: null,
      loading: true,
      needsSetup: false,
      billingUiEnabled: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });
    await renderShell();
    ensureFreshAccessTokenMock.mockClear();

    act(() => {
      window.dispatchEvent(new Event("focus"));
    });
    const visibilityStateSpy = vi
      .spyOn(document, "visibilityState", "get")
      .mockReturnValue("visible");
    try {
      act(() => {
        document.dispatchEvent(new Event("visibilitychange"));
      });
    } finally {
      visibilityStateSpy.mockRestore();
    }

    expect(ensureFreshAccessTokenMock).not.toHaveBeenCalled();
  });

  it("unmount removes focus + visibilitychange listeners", async () => {
    mockSignedInUser();
    // Render via act() so we get back the unmount function returned
    // by RTL's render. renderShell() doesn't expose it, so we
    // inline the render here.
    const { render, act: rtlAct } = await import("@testing-library/react");
    let unmount: () => void = () => {};
    await rtlAct(async () => {
      const result = render(
        <AppShell>
          <p>page body</p>
        </AppShell>,
      );
      unmount = result.unmount;
    });

    // Sanity: while mounted, focus fires the spy.
    ensureFreshAccessTokenMock.mockClear();
    act(() => {
      window.dispatchEvent(new Event("focus"));
    });
    expect(ensureFreshAccessTokenMock).toHaveBeenCalledTimes(1);

    // After unmount: events fire nothing.
    ensureFreshAccessTokenMock.mockClear();
    act(() => {
      unmount();
    });
    act(() => {
      window.dispatchEvent(new Event("focus"));
    });
    const visibilityStateSpy = vi
      .spyOn(document, "visibilityState", "get")
      .mockReturnValue("visible");
    try {
      act(() => {
        document.dispatchEvent(new Event("visibilitychange"));
      });
    } finally {
      visibilityStateSpy.mockRestore();
    }
    expect(ensureFreshAccessTokenMock).not.toHaveBeenCalled();
  });
});
