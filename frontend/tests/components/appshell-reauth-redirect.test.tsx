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

  it("redirects only once per logged-out episode (no reason-clear re-fire)", async () => {
    currentPathname = "/transactions";
    mockSignedOut("expired");

    await renderShell();

    // A stale re-run after clearAuthExitReason() must NOT overwrite the URL
    // with a reason-less redirect.
    expect(replaceMock).toHaveBeenCalledTimes(1);
    expect(replaceMock).not.toHaveBeenCalledWith("/login?returnTo=%2Ftransactions");
  });
});
