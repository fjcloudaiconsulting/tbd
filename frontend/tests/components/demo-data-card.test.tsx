/**
 * DemoDataCard tests (L3.3 in-app synthetic reseed).
 *
 * Coverage:
 *   - Renders nothing for non-owner users (defensive role gate).
 *   - Owner sees Load + Replace buttons and the help copy.
 *   - "Load demo data" calls /seed-demo?empty_org_only=true.
 *   - 409 from /seed-demo on Load surfaces a soft note (not error).
 *   - "Replace" reveals the typed-confirm panel; confirm button is
 *     disabled until the phrase matches AND the ack checkbox is on.
 *   - Replace chains /orgs/data/reset then /seed-demo?empty_org_only=false.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { User } from "@/lib/types";

import DemoDataCard from "@/components/settings/DemoDataCard";
import { apiFetch, ApiResponseError } from "@/lib/api";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("swr", async () => {
  const actual = await vi.importActual<typeof import("swr")>("swr");
  return { ...actual, mutate: vi.fn(async () => undefined) };
});

const pushMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, replace: vi.fn() }),
  usePathname: () => "/settings/organization",
}));

function makeUser(overrides: Partial<User> = {}): User {
  return {
    id: 1, username: "owner", email: "owner@example.com",
    first_name: null, last_name: null, phone: null, avatar_url: null,
    email_verified: true,
    role: "owner",
    org_id: 1, org_name: "Demo Org", billing_cycle_day: 1,
    is_superadmin: false, is_active: true, mfa_enabled: false,
    password_set: true,
    allow_manual_balance_adjustment: false,
    onboarded_at: null,
    subscription_status: null, subscription_plan: null, trial_end: null,
    ...overrides,
  };
}

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  pushMock.mockReset();
});

describe("DemoDataCard", () => {
  it("renders nothing for non-owners", () => {
    const { container } = render(<DemoDataCard user={makeUser({ role: "admin" })} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders Load and Replace buttons for owners", () => {
    render(<DemoDataCard user={makeUser()} />);
    expect(screen.getByTestId("settings-demo-load")).toBeInTheDocument();
    expect(screen.getByTestId("settings-demo-replace-open")).toBeInTheDocument();
  });

  it('"Load demo data" calls seed-demo with empty_org_only=true', async () => {
    vi.mocked(apiFetch).mockResolvedValue({
      accounts_created: 2,
      transactions_created: 14,
    });
    render(<DemoDataCard user={makeUser()} />);
    fireEvent.click(screen.getByTestId("settings-demo-load"));
    await waitFor(() => {
      expect(vi.mocked(apiFetch)).toHaveBeenCalledWith(
        "/api/v1/users/me/onboarding/seed-demo?empty_org_only=true",
        expect.objectContaining({ method: "POST" }),
      );
    });
    await waitFor(() =>
      expect(screen.getByTestId("settings-demo-info")).toHaveTextContent(
        /2 accounts, 14 transactions/i,
      ),
    );
  });

  it("surfaces a soft note on 409 (not a hard error)", async () => {
    vi.mocked(apiFetch).mockRejectedValue(
      new ApiResponseError(409, "org_has_data"),
    );
    render(<DemoDataCard user={makeUser()} />);
    fireEvent.click(screen.getByTestId("settings-demo-load"));
    await waitFor(() =>
      expect(screen.getByTestId("settings-demo-info")).toHaveTextContent(
        /already has data/i,
      ),
    );
    // Hard-error placeholder should NOT render.
    expect(screen.queryByTestId("settings-demo-error")).not.toBeInTheDocument();
  });

  it("Replace panel keeps confirm disabled until phrase and ack are both correct", async () => {
    render(<DemoDataCard user={makeUser()} />);
    fireEvent.click(screen.getByTestId("settings-demo-replace-open"));
    const confirmBtn = await screen.findByTestId("settings-demo-replace-confirm");
    expect(confirmBtn).toBeDisabled();

    // Only the phrase, no ack → still disabled.
    fireEvent.change(screen.getByTestId("settings-demo-replace-input"), {
      target: { value: "load demo data" },
    });
    expect(confirmBtn).toBeDisabled();

    // Ack but wrong phrase → still disabled.
    fireEvent.change(screen.getByTestId("settings-demo-replace-input"), {
      target: { value: "nope" },
    });
    fireEvent.click(screen.getByTestId("settings-demo-replace-ack"));
    expect(confirmBtn).toBeDisabled();

    // Both correct → enabled.
    fireEvent.change(screen.getByTestId("settings-demo-replace-input"), {
      target: { value: "load demo data" },
    });
    expect(confirmBtn).not.toBeDisabled();
  });

  it("Replace chains /orgs/data/reset then /seed-demo?empty_org_only=false", async () => {
    const calls: string[] = [];
    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      calls.push(url);
      if (url.startsWith("/api/v1/orgs/data/reset")) {
        return Promise.resolve({ deleted_rows_by_table: {} });
      }
      return Promise.resolve({ accounts_created: 2, transactions_created: 14 });
    }) as never);

    render(<DemoDataCard user={makeUser()} />);
    fireEvent.click(screen.getByTestId("settings-demo-replace-open"));
    fireEvent.change(
      await screen.findByTestId("settings-demo-replace-input"),
      { target: { value: "load demo data" } },
    );
    fireEvent.click(screen.getByTestId("settings-demo-replace-ack"));
    fireEvent.click(screen.getByTestId("settings-demo-replace-confirm"));

    await waitFor(() => {
      expect(calls).toContain("/api/v1/orgs/data/reset");
      expect(calls).toContain(
        "/api/v1/users/me/onboarding/seed-demo?empty_org_only=false",
      );
    });
    // Reset must come BEFORE seed.
    expect(calls.indexOf("/api/v1/orgs/data/reset")).toBeLessThan(
      calls.indexOf(
        "/api/v1/users/me/onboarding/seed-demo?empty_org_only=false",
      ),
    );
    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/dashboard"));
  });
});
