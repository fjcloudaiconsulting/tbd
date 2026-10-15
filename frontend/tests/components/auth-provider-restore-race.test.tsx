// TBD-623 F9: the restore/login race, against the REAL lib/api setters.
// auth-provider.test.tsx stubs setAccessToken as a bare vi.fn() for the whole
// file, so it can never see a refusal. Here only apiFetch is mocked.
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { AuthProvider, useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, getAccessToken, signInWithAccessToken } from "@/lib/api";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

function b64(s: string): string {
  return btoa(s).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
}
const tok = (sub: string, org: number) =>
  `${b64("{}")}.${b64(JSON.stringify({ sub, org_id: org, exp: 4102444800 }))}.s`;

const USER_A = { id: 5, email: "a@example.com", org_id: 7 };

function Harness() {
  const { user, loading, login } = useAuth();
  return (
    <div>
      <div data-testid="loading">{String(loading)}</div>
      <div data-testid="user">{user?.email ?? "none"}</div>
      <button onClick={() => login("a", "pw").catch(() => {})}>Login</button>
    </div>
  );
}

describe("AuthProvider restore race (real setters)", () => {
  const apiFetchMock = vi.mocked(apiFetch);

  beforeEach(() => {
    apiFetchMock.mockReset();
    signInWithAccessToken(null);
  });

  afterEach(() => signInWithAccessToken(null));

  it("F9: login arms A while restore's refresh is in flight; restore then resolving with X changes nothing and clears loading", async () => {
    const a = tok("5", 7);
    const x = tok("9", 11);
    let resolveRefresh!: (v: unknown) => void;
    const refreshGate = new Promise((r) => (resolveRefresh = r));
    let meCalls = 0;

    apiFetchMock.mockImplementation((async (path: string) => {
      if (path === "/api/v1/auth/status") return { needs_setup: false };
      if (path === "/api/v1/auth/refresh") return refreshGate;
      if (path === "/api/v1/auth/login") return { access_token: a };
      if (path === "/api/v1/auth/me") {
        meCalls++;
        return USER_A;
      }
      throw new Error(`unexpected ${path}`);
    }) as never);

    render(
      <AuthProvider>
        <Harness />
      </AuthProvider>,
    );
    // restore is parked on /refresh; the login form still accepts a submit.
    await waitFor(() =>
      expect(apiFetchMock).toHaveBeenCalledWith("/api/v1/auth/refresh", { method: "POST" }),
    );
    fireEvent.click(screen.getByText("Login"));
    await waitFor(() =>
      expect(screen.getByTestId("user")).toHaveTextContent(USER_A.email),
    );
    expect(getAccessToken()).toBe(a);

    await act(async () => {
      resolveRefresh({ access_token: x });
    });

    await waitFor(() =>
      expect(screen.getByTestId("loading")).toHaveTextContent("false"),
    );
    expect(getAccessToken()).toBe(a);
    expect(screen.getByTestId("user")).toHaveTextContent(USER_A.email);
    expect(meCalls).toBe(1); // /me was never called with X
  });
});
