import { render, waitFor } from "@testing-library/react";

import DashboardPage from "@/app/dashboard/page";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";

/**
 * Regression: PR #226 wrapped the dashboard On Track hero row in a
 * `<TourAnchor id="dashboard.on-track-tile">` WITHOUT `as="child"`.
 * TourAnchor's default wrapper renders a bare `<span>` around the
 * children, which makes the structural ancestor of the hero an inline
 * element while its direct child is a block-level flex container.
 *
 * As a direct child of `<div className="space-y-5">`, the bare
 * `<span>` receives Tailwind's `margin-block-end: 1.25rem` rule but
 * CSS ignores vertical margins on inline elements. The vertical
 * rhythm between the period nav, the on-track hero, and the
 * accounts/forecast grid below therefore collapsed on the production
 * dashboard.
 *
 * Contract: the data-tour-id MUST be on the flex container itself
 * (TourAnchor's `as="child"` path clones the child and injects the
 * attribute on it). There MUST NOT be a wrapping `<span
 * data-tour-id="dashboard.on-track-tile">` around the row.
 *
 * Every other TourAnchor on this page already uses `as="child"`; this
 * anchor was the lone exception. Do not remove `as="child"` from this
 * marker without re-running a production build visual diff.
 */

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

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

const stableRouter = { push: vi.fn(), replace: vi.fn() };
vi.mock("next/navigation", () => ({
  useRouter: () => stableRouter,
  usePathname: () => "/dashboard",
}));

const USER = {
  id: 1,
  username: "u",
  email: "u@x.io",
  first_name: null,
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

function mockEmptyDashboard() {
  vi.mocked(apiFetch).mockImplementation(((url: string) => {
    if (url === "/api/v1/accounts") return Promise.resolve([]);
    if (url === "/api/v1/categories") return Promise.resolve([]);
    if (url === "/api/v1/budgets") return Promise.resolve([]);
    if (url === "/api/v1/settings/billing-cycle")
      return Promise.resolve({ billing_cycle_day: 1 });
    if (url === "/api/v1/settings/billing-period")
      return Promise.resolve({ id: 1, start_date: "2026-05-01", end_date: null });
    if (url === "/api/v1/settings/billing-periods")
      return Promise.resolve([{ id: 1, start_date: "2026-05-01", end_date: null }]);
    if (url.startsWith("/api/v1/transactions")) return Promise.resolve({ items: [], total: 0, limit: 200, offset: 0 });
    if (url.startsWith("/api/v1/forecast-plans/current"))
      return Promise.resolve(null);
    return Promise.resolve({});
  }) as never);
}

describe("DashboardPage — dashboard.on-track-tile TourAnchor shape (prod alignment regression)", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    window.history.pushState({}, "", "/dashboard");
    vi.mocked(useAuth).mockReturnValue({
      user: USER as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);
    mockEmptyDashboard();
  });

  it("places data-tour-id directly on the on-track wrapper (not on a wrapping span)", async () => {
    const { container } = render(<DashboardPage />);

    // Wait for the dashboard to finish loading and render the on-track
    // anchor. The wrapper is the element we'll find by its tour id.
    await waitFor(() => {
      const tagged = container.querySelector('[data-tour-id="dashboard.on-track-tile"]');
      expect(tagged).not.toBeNull();
    });

    const tagged = container.querySelector(
      '[data-tour-id="dashboard.on-track-tile"]',
    ) as HTMLElement;

    // Must be the block container itself, not a wrapping span.
    // (PR #226 regression: TourAnchor without `as="child"` wraps the
    // child in an inline span, which collapses block-level vertical
    // margins from the parent `space-y-5` rule.)
    expect(tagged.tagName).toBe("DIV");

    // Wrapper must be positioned-relative so the in-card help marker
    // can dock in the top-right corner via absolute positioning.
    // Owner spec 2026-05-13: "tooltip inside the cards."
    expect(tagged.className).toContain("relative");

    // Defensive: ensure no sibling span carries the tour id either.
    const taggedSpans = container.querySelectorAll(
      'span[data-tour-id="dashboard.on-track-tile"]',
    );
    expect(taggedSpans.length).toBe(0);
  });

  it("keeps the on-track flex row as a direct child of the space-y-5 container (prod vertical rhythm guard)", async () => {
    const { container } = render(<DashboardPage />);

    await waitFor(() => {
      expect(
        container.querySelector('[data-tour-id="dashboard.on-track-tile"]'),
      ).not.toBeNull();
    });

    const tagged = container.querySelector(
      '[data-tour-id="dashboard.on-track-tile"]',
    ) as HTMLElement;

    const parent = tagged.parentElement as HTMLElement;
    // The flex row's parent is the dashboard's `<div className="space-y-5">`
    // wrapper. If a future change re-introduces a wrapping element, this
    // assertion fails and forces a deliberate review.
    expect(parent.className).toContain("space-y-5");
  });

  it("does not double-wrap any other dashboard TourAnchor that contains block-level layout", async () => {
    const { container } = render(<DashboardPage />);

    // Wait for the page to render.
    await waitFor(() => {
      expect(
        container.querySelector('[data-tour-id="dashboard.on-track-tile"]'),
      ).not.toBeNull();
    });

    // Each known dashboard tour id should be on a real element (DIV /
    // A for the import CTA), never on a tour-anchor wrapper span.
    const ids = [
      "dashboard.header",
      "dashboard.period-nav",
      "dashboard.on-track-tile",
      "dashboard.account-forecast",
    ];
    for (const id of ids) {
      const el = container.querySelector(`[data-tour-id="${id}"]`);
      expect(el, `data-tour-id="${id}" missing`).not.toBeNull();
      const tag = (el as HTMLElement).tagName;
      // SPAN is only acceptable when it's the explicit TourAnchor
      // wrapper (data-testid="tour-anchor"). For these layout-sensitive
      // ids, the marker MUST live on the real child element.
      expect(
        tag,
        `data-tour-id="${id}" should be on a layout element, not a TourAnchor wrapper span`,
      ).not.toBe("SPAN");
    }
  });
});
