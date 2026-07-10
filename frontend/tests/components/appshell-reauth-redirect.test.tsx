import { useState } from "react";
import { act, render } from "@testing-library/react";

import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";

vi.mock("@/lib/logger", () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
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

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    apiFetch: vi.fn(async () => [] as never),
    ensureFreshAccessToken: vi.fn(async () => undefined),
  };
});

// Mutable pathname + a stable replace spy so we can assert the exact
// /login URL AppShell builds on the redirect.
let currentPathname = "/transactions";
const replaceMock = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: replaceMock }),
  usePathname: () => currentPathname,
}));

async function renderShell() {
  await act(async () => {
    render(
      <AppShell>
        <p>page body</p>
      </AppShell>,
    );
  });
}

describe("AppShell — graceful re-auth redirect", () => {
  const useAuthMock = vi.mocked(useAuth);
  const clearAuthExitReason = vi.fn();

  beforeEach(() => {
    replaceMock.mockReset();
    clearAuthExitReason.mockReset();
    useAuthMock.mockReset();
    currentPathname = "/transactions";
  });

  function mockSignedOut(authExitReason: "expired" | "manual" | null) {
    useAuthMock.mockReturnValue({
      user: null,
      loading: false,
      needsSetup: false,
      billingUiEnabled: false,
      authExitReason,
      clearAuthExitReason,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);
  }

  it("session EXPIRED: redirects with returnTo of the current page AND reason=expired", async () => {
    currentPathname = "/transactions";
    mockSignedOut("expired");

    await renderShell();

    expect(replaceMock).toHaveBeenCalledTimes(1);
    expect(replaceMock).toHaveBeenCalledWith(
      "/login?returnTo=%2Ftransactions&reason=expired",
    );
    // Consumed once so it can't leak into a later redirect.
    expect(clearAuthExitReason).toHaveBeenCalledTimes(1);
  });

  it("manual LOGOUT: redirects with reason=logout and NO returnTo", async () => {
    currentPathname = "/transactions";
    mockSignedOut("manual");

    await renderShell();

    expect(replaceMock).toHaveBeenCalledTimes(1);
    expect(replaceMock).toHaveBeenCalledWith("/login?reason=logout");
    expect(clearAuthExitReason).toHaveBeenCalledTimes(1);
  });

  it("fresh deep-link visit while logged out: returnTo only, no reason", async () => {
    currentPathname = "/reports/5";
    mockSignedOut(null);

    await renderShell();

    expect(replaceMock).toHaveBeenCalledTimes(1);
    expect(replaceMock).toHaveBeenCalledWith("/login?returnTo=%2Freports%2F5");
  });

  it("never sets returnTo to the /login route itself", async () => {
    currentPathname = "/login";
    mockSignedOut(null);

    await renderShell();

    expect(replaceMock).toHaveBeenCalledTimes(1);
    expect(replaceMock).toHaveBeenCalledWith("/login");
  });

  it("does not set returnTo for the /setup auth route", async () => {
    currentPathname = "/setup";
    mockSignedOut("expired");

    await renderShell();

    // reason still carried, but no returnTo pointing back at /setup.
    expect(replaceMock).toHaveBeenCalledWith("/login?reason=expired");
  });

  // A minimal signed-in user for the second-episode test. AppShell reads a
  // handful of fields when it renders the full shell; onboarded_at is a
  // non-null string so the onboarding-bounce effect stays dormant.
  const SIGNED_IN_USER = {
    id: 1,
    username: "alice",
    first_name: "Alice",
    org_name: "Test Org",
    is_superadmin: false,
    role: "owner",
    onboarded_at: "2026-01-01T00:00:00Z",
  };

  // Stateful harness: authExitReason + user live in real React state so the
  // production clearAuthExitReason() consume (setState → re-render →
  // effect re-run) genuinely happens, exercising the reauthRedirectedRef
  // fire-once guard instead of mocking it away. Imperative setters are
  // captured so a test can drive user transitions across episodes.
  let clearCalls = 0;
  let setUserExternal: (u: unknown) => void = () => {};
  let setReasonExternal: (r: "expired" | "manual" | null) => void = () => {};

  function Harness({
    initialUser,
    initialReason,
  }: {
    initialUser: unknown;
    initialReason: "expired" | "manual" | null;
  }) {
    const [user, setUser] = useState<unknown>(initialUser);
    const [reason, setReason] = useState<"expired" | "manual" | null>(
      initialReason,
    );
    setUserExternal = setUser;
    setReasonExternal = setReason;
    // Parent renders before child, so seeding the mock here means AppShell
    // reads the current state on every (re-)render.
    useAuthMock.mockReturnValue({
      user,
      loading: false,
      needsSetup: false,
      billingUiEnabled: false,
      features: { reports: false, plans: false, customDashboard: false },
      authExitReason: reason,
      clearAuthExitReason: () => {
        clearCalls += 1;
        // Real consume: flip the reason to null exactly like AuthProvider.
        setReason(null);
      },
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);
    return (
      <AppShell>
        <p>page body</p>
      </AppShell>
    );
  }

  beforeEach(() => {
    clearCalls = 0;
  });

  it("fires exactly once per logged-out episode even though clearAuthExitReason re-renders", async () => {
    currentPathname = "/transactions";

    await act(async () => {
      render(<Harness initialUser={null} initialReason="expired" />);
    });

    // clearAuthExitReason genuinely flipped reason expired→null and forced a
    // re-render; the ref guard must have swallowed the second effect run.
    expect(clearCalls).toBe(1);
    expect(replaceMock).toHaveBeenCalledTimes(1);
    expect(replaceMock).toHaveBeenCalledWith(
      "/login?returnTo=%2Ftransactions&reason=expired",
    );
    // The reason-less redirect the stale re-run WOULD have produced never fires.
    expect(replaceMock).not.toHaveBeenCalledWith(
      "/login?returnTo=%2Ftransactions",
    );
  });

  it("re-arms for a SECOND logged-out episode after the user signs back in", async () => {
    currentPathname = "/transactions";

    // Episode 1: session expired → one correct redirect, reason consumed.
    await act(async () => {
      render(<Harness initialUser={null} initialReason="expired" />);
    });
    expect(replaceMock).toHaveBeenCalledTimes(1);
    expect(replaceMock).toHaveBeenLastCalledWith(
      "/login?returnTo=%2Ftransactions&reason=expired",
    );

    // Sign back in: the effect resets reauthRedirectedRef and issues no
    // redirect while a user is present.
    await act(async () => {
      setUserExternal(SIGNED_IN_USER);
    });
    expect(replaceMock).toHaveBeenCalledTimes(1);

    // Episode 2: expire again. Because the ref reset on sign-in, a second
    // redirect must fire (deleting the reset branch would leave this at 1).
    await act(async () => {
      setUserExternal(null);
      setReasonExternal("expired");
    });
    expect(replaceMock).toHaveBeenCalledTimes(2);
    expect(replaceMock).toHaveBeenLastCalledWith(
      "/login?returnTo=%2Ftransactions&reason=expired",
    );
  });
});
