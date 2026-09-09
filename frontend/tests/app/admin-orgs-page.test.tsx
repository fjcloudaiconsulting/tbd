import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import AdminOrgsPage from "@/app/admin/orgs/page";
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

// ⚠ TBD-503: a `mockResolvedValueOnce` QUEUE is consumed in CALL ORDER
// regardless of arguments, so ANY new fetch above the component under test
// eats this page's first queued response and desynchronises everything after
// it — failing with a symptom that points at the wrong file entirely. That is
// exactly what happened here: an interim design mounted OrgCurrencyProvider
// inside `AppShell`, and five unrelated suites broke at once.
//
// That provider now mounts in the ROOT LAYOUT (`OrgCurrencyBoundary`), which
// no RTL test renders, so no accounts fetch reaches these pages today. The
// helper stays regardless: the queue's order-dependence is the defect, and it
// is one fetch away from biting again. Tracked for the rest of the suite in
// TBD-504.
//
// Path- and METHOD-aware instead. Two rules that matter:
//   - it THROWS on an unmatched path rather than returning a default, so an
//     unexpected call stays as loud as the queue made it. A catch-all
//     `Promise.resolve([])` would make every un-mocked endpoint fail open.
//   - it branches on `init?.method`, because the page issues GET and PUT/DELETE
//     against the SAME url and a url-only handler would serve the read payload
//     to the write and mask a body-shape bug.
type Route = { path: string; method?: string; body: unknown };
function serve(routes: Route[]) {
  vi.mocked(apiFetch).mockImplementation(
    (async (path: unknown, init?: { method?: string }) => {
      const url = String(path);
      const method = init?.method ?? "GET";
      for (const r of routes) {
        if (url.startsWith(r.path) && (r.method ?? "GET") === method) {
          if (r.body instanceof Error) throw r.body;
          return r.body as never;
        }
      }
      throw new Error(`unmocked ${method} ${url}`);
    }) as never,
  );
}

const replaceMock = vi.fn();
const currentSearchParams = new URLSearchParams();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: replaceMock }),
  usePathname: () => "/admin/orgs",
  useSearchParams: () => currentSearchParams,
}));


const SUPERADMIN = {
  id: 1, username: "root", email: "root@platform.io",
  first_name: null, last_name: null, phone: null, avatar_url: null,
  email_verified: true, role: "owner", org_id: 1, org_name: "Platform",
  billing_cycle_day: 1, is_superadmin: true, is_active: true,
  mfa_enabled: false, subscription_status: null, subscription_plan: null,
  trial_end: null,
};

describe("AdminOrgsPage", () => {
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

  it("renders the orgs table from the API", async () => {
    serve([{ path: "/api/v1/admin/orgs", body: {
      items: [
        {
          id: 10, name: "Acme", plan_slug: "free",
          subscription_status: "trialing", trial_end: "2026-05-15",
          user_count: 3, active_user_count: 2,
          created_at: "2026-04-15T10:00:00",
          last_user_created_at: "2026-04-30T10:00:00",
        },
      ],
      total: 1, limit: 50, offset: 0,
    } }]);

    render(<AdminOrgsPage />);

    await screen.findByText("Acme");
    expect(screen.getByText("free")).toBeInTheDocument();
    expect(screen.getByText("trialing")).toBeInTheDocument();
    expect(screen.getByText("2 / 3")).toBeInTheDocument();
  });

  it("redirects non-superadmin users without orgs.view away from the page", async () => {
    useAuthMock.mockReturnValue({
      user: { ...SUPERADMIN, is_superadmin: false } as never,
      loading: false, needsSetup: false,
      login: vi.fn(), register: vi.fn(), logout: vi.fn(), refreshMe: vi.fn(),
    });

    render(<AdminOrgsPage />);

    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/dashboard");
    });
  });

  it("renders for a non-superadmin who carries orgs.view in permissions", async () => {
    serve([{ path: "/api/v1/admin/orgs", body: {
      items: [
        {
          id: 11, name: "Beta Co", plan_slug: "free",
          subscription_status: "active", trial_end: null,
          user_count: 1, active_user_count: 1,
          created_at: "2026-04-15T10:00:00",
          last_user_created_at: "2026-04-15T10:00:00",
        },
      ],
      total: 1, limit: 50, offset: 0,
    } }]);
    useAuthMock.mockReturnValue({
      user: {
        ...SUPERADMIN,
        is_superadmin: false,
        permissions: ["orgs.view"],
      } as never,
      loading: false, needsSetup: false,
      login: vi.fn(), register: vi.fn(), logout: vi.fn(), refreshMe: vi.fn(),
    });

    render(<AdminOrgsPage />);

    await screen.findByText("Beta Co");
    expect(replaceMock).not.toHaveBeenCalledWith("/dashboard");
  });

  it("sweeps expired overrides and shows the deleted count", async () => {
    // ⚠ The sweep path STARTS WITH the list path, so a url-only handler would
    // serve the list payload to the sweep. The method branch is what keeps
    // them apart: GET list, POST sweep.
    serve([
      { path: "/api/v1/admin/orgs/feature-overrides/sweep-expired", method: "POST", body: { deleted_count: 3 } },
      { path: "/api/v1/admin/orgs", body: { items: [], total: 0, limit: 50, offset: 0 } },
    ]);

    render(<AdminOrgsPage />);

    const button = await screen.findByRole("button", {
      name: /sweep expired overrides/i,
    });
    fireEvent.click(button);

    const confirm = await screen.findByRole("button", { name: /^Sweep$/ });
    fireEvent.click(confirm);

    await waitFor(() => {
      expect(apiFetchMock).toHaveBeenCalledWith(
        "/api/v1/admin/orgs/feature-overrides/sweep-expired",
        { method: "POST" },
      );
    });
    await screen.findByText("Removed 3 expired overrides.");
  });
});
