/**
 * The header "Hide balances" toggle (TBD-527, fence F6).
 *
 * ⚠ The accessible name is STABLE and the state rides `aria-pressed`. A label
 * that flips ("Hide" / "Show", the way `ThemeToggle` does) makes a toggle
 * button announce its action instead of its state, and breaks any lookup by
 * name after the first click.
 */
import { act, fireEvent, render, screen } from "@testing-library/react";

import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";
import { setBalancesHidden } from "@/lib/format";

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<typeof import("@/components/auth/AuthProvider")>(
    "@/components/auth/AuthProvider",
  );
  return { ...actual, useAuth: vi.fn() };
});

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/dashboard",
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn(async () => [] as never) };
});

const USER = {
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

function mockAuth(user: Record<string, unknown> | null) {
  vi.mocked(useAuth).mockReturnValue({
    user: user as never,
    loading: false,
    needsSetup: false,
    billingUiEnabled: true,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn(),
  });
}

async function renderShell() {
  let result!: ReturnType<typeof render>;
  await act(async () => {
    result = render(
      <AppShell>
        <p>page body</p>
      </AppShell>,
    );
  });
  return result;
}

beforeEach(() => {
  setBalancesHidden(false);
  window.localStorage.clear();
});

describe("F6: Hide balances toggle", () => {
  it("keeps one accessible name while aria-pressed flips, and persists the choice", async () => {
    mockAuth(USER);
    await renderShell();

    const toggle = screen.getByRole("button", { name: "Hide balances" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");

    fireEvent.click(toggle);
    const pressed = screen.getByRole("button", { name: "Hide balances" });
    expect(pressed).toHaveAttribute("aria-pressed", "true");
    expect(window.localStorage.getItem("tbd-hide-balances")).toBe("1");

    fireEvent.click(pressed);
    expect(screen.getByRole("button", { name: "Hide balances" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    expect(window.localStorage.getItem("tbd-hide-balances")).toBe("0");
  });

  it("survives a remount pressed", async () => {
    mockAuth(USER);
    const first = await renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Hide balances" }));
    first.unmount();

    await renderShell();
    expect(screen.getByRole("button", { name: "Hide balances" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("is not rendered while logged out", async () => {
    mockAuth(null);
    await renderShell();
    expect(screen.queryByRole("button", { name: "Hide balances" })).toBeNull();
  });
});
