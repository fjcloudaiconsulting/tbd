// TBD-285. Generation's catch-up keeps back-dated occurrences on their real
// dates, and says so: the Generate toast names how many rows landed before
// the current billing cycle. Drives the real Generate click.
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

import RecurringPage from "@/app/recurring/page";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<
    typeof import("@/components/auth/AuthProvider")
  >("@/components/auth/AuthProvider");
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
  id: 1, username: "u", email: "u@x.io", first_name: null, last_name: null,
  phone: null, avatar_url: null, email_verified: true, role: "owner",
  org_id: 1, org_name: "Acme", billing_cycle_day: 1, is_superadmin: false,
  is_active: true, mfa_enabled: false, password_set: true,
  subscription_status: null, subscription_plan: null, trial_end: null,
  allow_manual_balance_adjustment: false,
};

function mockGenerate(backfilled: number) {
  vi.mocked(apiFetch).mockImplementation(((url: string) => {
    if (url === "/api/v1/recurring/generate") {
      return Promise.resolve({
        generated: 3, settled: 0, pending: 3, backfilled, period_end: "2026-09-30",
      });
    }
    return Promise.resolve([]);
  }) as never);
}

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  window.localStorage.clear();
  vi.mocked(useAuth).mockReturnValue({
    user: USER as never, loading: false, needsSetup: false,
    login: vi.fn(), register: vi.fn(), logout: vi.fn(), refreshMe: vi.fn(),
  } as never);
});

async function generateAndSettle() {
  fireEvent.click(await screen.findByRole("button", { name: /Generate this period/ }));
  await screen.findByText(/Generated 3 transaction\(s\)/);
  // Settle point: handleGenerate awaits reload() after setting the toast.
  await waitFor(() =>
    expect(vi.mocked(apiFetch).mock.calls.filter(([u]) => u === "/api/v1/recurring")).toHaveLength(2),
  );
}

describe("recurring page — Generate toast reports back-filled rows (TBD-285)", () => {
  it("fence: names the rows dated before the current cycle", async () => {
    mockGenerate(2);
    render(<RecurringPage />);
    await generateAndSettle();
    // Kills: a sentence that is never on.
    expect(
      screen.getByText(/Generated 3 transaction\(s\).*2 of them are dated before the current billing cycle\./),
    ).toBeInTheDocument();
  });

  it("fence: uses the singular at 1", async () => {
    mockGenerate(1);
    render(<RecurringPage />);
    await generateAndSettle();
    // Kills: "1 of them are dated".
    expect(
      screen.getByText(/1 of them is dated before the current billing cycle\./),
    ).toBeInTheDocument();
  });

  it("fence: says nothing about back-filling at 0", async () => {
    mockGenerate(0);
    render(<RecurringPage />);
    await generateAndSettle();
    // Kills: a sentence that is always on.
    expect(screen.queryByText(/dated before the current billing cycle/)).not.toBeInTheDocument();
  });
});
