import { act, render, screen, within } from "@testing-library/react";

import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";

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
  // /dashboard hides the per-page Add Transaction CTA so the header
  // chrome we are testing renders consistently across cases.
  usePathname: () => "/dashboard",
}));

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
  await act(async () => {
    render(
      <AppShell>
        <p>page body</p>
      </AppShell>,
    );
  });
}

function mockAuth(user: Record<string, unknown>) {
  vi.mocked(useAuth).mockReturnValue({
    user: user as never,
    loading: false,
    needsSetup: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn(),
  });
}

describe("AppShell header + footer polish (L5.4)", () => {
  beforeEach(() => {
    vi.mocked(useAuth).mockReset();
  });

  it("renders the brand lockup in the sidebar instead of an inline 'TBD' string", async () => {
    mockAuth(BASE_USER);
    await renderShell();

    // The sidebar wordmark now comes from <Logo />, which exposes the
    // accessible brand name on the wordmark span. The previous inline
    // "TBD" text node lived only inside the dashboard link.
    const brandLink = screen.getByRole("link", {
      name: /the better decision/i,
    });
    expect(brandLink).toHaveAttribute("href", "/dashboard");
  });

  it("renders the footer with Privacy / Terms / Help / contact and no em-dash", async () => {
    mockAuth(BASE_USER);
    const { container } = await act(async () => {
      const result = render(
        <AppShell>
          <p>page body</p>
        </AppShell>,
      );
      return result;
    });
    const footer = container.querySelector("nav[aria-label='App footer']");
    expect(footer).not.toBeNull();
    const scoped = within(footer as HTMLElement);
    expect(scoped.getByRole("link", { name: /^privacy$/i })).toHaveAttribute(
      "href",
      "/privacy",
    );
    expect(scoped.getByRole("link", { name: /^terms$/i })).toHaveAttribute(
      "href",
      "/terms",
    );
    expect(scoped.getByRole("link", { name: /^help$/i })).toHaveAttribute(
      "href",
      "/docs",
    );
    expect(
      scoped.getByRole("link", { name: /hello@thebetterdecision\.com/i }),
    ).toHaveAttribute("href", "mailto:hello@thebetterdecision.com");
    expect(container.textContent).not.toMatch(/—/);
  });

  it("does not surface 'PFV' or 'pfv2' anywhere in the rendered shell", async () => {
    mockAuth(BASE_USER);
    const { container } = await act(async () => {
      return render(
        <AppShell>
          <p>page body</p>
        </AppShell>,
      );
    });
    // Rendered text + accessible attributes only. localStorage keys
    // and event names are not user-visible and stay out of scope.
    expect(container.textContent).not.toMatch(/\bPFV\b/);
    expect(container.textContent).not.toMatch(/\bpfv2?\b/i);
    const html = container.innerHTML;
    expect(html).not.toMatch(/\bPFV\b/);
    expect(html).not.toMatch(/\bpfv2\b/i);
  });

  it("still shows the theme toggle and the docs link in the header", async () => {
    mockAuth(BASE_USER);
    await renderShell();

    expect(screen.getByRole("button", { name: /switch to (light|dark) mode/i }))
      .toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^docs$/i })).toHaveAttribute(
      "href",
      "/docs",
    );
  });

  it("renders the trial banner when the user is on a trial (snapshot)", async () => {
    // Pick a date well past today (the trialing branch needs >3 days
    // left to render the calm banner variant). The fixed date below is
    // 12 months ahead of 2026-05-12, so the banner stays stable
    // regardless of CI clock skew.
    const trialUser = {
      ...BASE_USER,
      subscription_status: "trialing",
      subscription_plan: "pro",
      trial_end: "2099-12-31",
    };
    mockAuth(trialUser);
    await renderShell();

    // The pro-trial banner renders the "Pro Trial" label.
    expect(screen.getByText(/pro trial/i)).toBeInTheDocument();
  });

  it("hides the trial banner for a user without subscription metadata", async () => {
    mockAuth(BASE_USER);
    await renderShell();
    expect(screen.queryByText(/pro trial/i)).toBeNull();
    expect(screen.queryByText(/trial ending/i)).toBeNull();
  });

  it("renders the header right-aligned on lg+ to address the lopsided-left backlog item", async () => {
    mockAuth(BASE_USER);
    const { container } = await act(async () => {
      return render(
        <AppShell>
          <p>page body</p>
        </AppShell>,
      );
    });
    // Class-based assertion documents the intent. The full visual
    // story is in the Playwright screenshots attached to the PR.
    const header = container.querySelector("header");
    expect(header).not.toBeNull();
    expect((header as HTMLElement).className).toMatch(/lg:justify-end/);
  });
});
