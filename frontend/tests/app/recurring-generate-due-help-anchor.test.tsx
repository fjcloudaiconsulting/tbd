import { render, screen, waitFor } from "@testing-library/react";

import RecurringPage from "@/app/recurring/page";
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
  return {
    ...actual,
    useAuth: vi.fn(),
    AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  };
});

const stableRouter = { push: vi.fn(), replace: vi.fn() };
vi.mock("next/navigation", () => ({
  useRouter: () => stableRouter,
  usePathname: () => "/recurring",
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
  password_set: true,
  subscription_status: null,
  subscription_plan: null,
  trial_end: null,
  allow_manual_balance_adjustment: false,
};

function mockApi() {
  vi.mocked(apiFetch).mockImplementation(((url: string) => {
    if (url === "/api/v1/recurring") return Promise.resolve([]);
    return Promise.resolve({});
  }) as never);
}

describe("RecurringPage — header layout + Generate Due HelpAnchor", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    vi.mocked(useAuth).mockReturnValue({
      user: USER as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);
    mockApi();
  });

  it("renders a HelpAnchor next to the page title pointing at /docs#recurring", async () => {
    render(<RecurringPage />);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /Generate this period/ }),
      ).toBeInTheDocument(),
    );

    // aria-label includes "Help: Recurring transactions" so screen-
    // reader users pick up the topic association before the icon.
    const helpLink = screen.getByRole("link", { name: /Help: Recurring transactions/ });
    expect(helpLink).toHaveAttribute("href", "/docs#recurring");
    expect(helpLink).toHaveAttribute("target", "_blank");
    expect(helpLink).toHaveAttribute("rel", "noopener noreferrer");
    expect(helpLink).toHaveAttribute("data-section", "recurring");
  });

  it("uses the inline-title HelpAnchor variant (next to the page H1)", async () => {
    render(<RecurringPage />);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /Generate this period/ }),
      ).toBeInTheDocument(),
    );

    const helpLink = screen.getByTestId("help-anchor");
    // inline-title self-aligns to cap height of adjacent heading
    // text. card-corner would absolutely position, which is wrong
    // here since the surrounding flex row is not a relative card.
    expect(helpLink).toHaveAttribute("data-variant", "inline-title");
  });

  it("nests the HelpAnchor inside the H1 alongside the title text", async () => {
    // Geometric promise of the inline-title variant: the icon lives
    // INSIDE the heading element so it tracks the title across
    // breakpoints. If the HelpAnchor escaped to a sibling div, it
    // would no longer wrap with the title on mobile and the mobile
    // overflow regression would creep back in.
    render(<RecurringPage />);
    const heading = await screen.findByRole("heading", { name: /Recurring transactions/i, level: 1 });
    const helpLink = screen.getByTestId("help-anchor");
    expect(heading.contains(helpLink)).toBe(true);
  });

  it("stacks the header vertically on mobile and switches to a row at sm+", async () => {
    render(<RecurringPage />);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /Generate this period/ }),
      ).toBeInTheDocument(),
    );

    const header = screen.getByTestId("recurring-page-header");
    // Default (mobile) stacks the title cluster + Generate Due
    // button vertically so the row never overflows on 375px-wide
    // screens.
    expect(header.className).toMatch(/\bflex-col\b/);
    expect(header.className).toMatch(/\bgap-3\b/);
    // sm+ flips to a row with the button right-aligned.
    expect(header.className).toMatch(/\bsm:flex-row\b/);
    expect(header.className).toMatch(/\bsm:items-center\b/);
    expect(header.className).toMatch(/\bsm:justify-between\b/);
  });

  it("keeps the Generate Due button clickable as a sibling of the title", async () => {
    render(<RecurringPage />);
    const button = await screen.findByRole("button", { name: /Generate this period/ });

    // Button stays a real <button> (not wrapped by the HelpAnchor
    // link), so the click handler still fires.
    expect(button.tagName).toBe("BUTTON");
    expect(button).not.toBeDisabled();
    // self-start at <sm so the button doesn't stretch full-width on
    // mobile; self-auto at sm+ so the row layout takes over.
    expect(button.className).toMatch(/\bself-start\b/);
    expect(button.className).toMatch(/\bsm:self-auto\b/);
  });
});
