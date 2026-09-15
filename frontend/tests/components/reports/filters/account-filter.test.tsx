import { renderWithSWR, act, fireEvent, screen, waitFor } from "../../../utils/render-with-swr";

import AccountFilter from "@/components/reports/filters/AccountFilter";
import { useAccounts } from "@/lib/hooks/use-accounts";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";

vi.mock("@/lib/api", () => ({
  apiFetch: vi.fn(),
}));

vi.mock("@/components/auth/AuthProvider", () => ({ useAuth: vi.fn() }));

const ACCOUNTS = [
  {
    id: 1,
    name: "Checking",
    account_type_id: 1,
    account_type_name: "Bank",
    account_type_slug: "bank",
    balance: 0,
    currency: "USD",
    is_active: true,
    close_day: null,
    is_default: true,
  },
  {
    id: 2,
    name: "Credit Card",
    account_type_id: 2,
    account_type_name: "Credit",
    account_type_slug: "credit",
    balance: 0,
    currency: "USD",
    is_active: true,
    close_day: null,
    is_default: false,
  },
];

describe("AccountFilter", () => {
  const apiFetchMock = vi.mocked(apiFetch);

  beforeEach(() => {
    apiFetchMock.mockReset();
    vi.mocked(useAuth).mockReturnValue({ user: { id: 1 }, loading: false } as never);
  });

  it("fetches accounts on mount and renders one chip per account", async () => {
    apiFetchMock.mockResolvedValueOnce(ACCOUNTS);

    renderWithSWR(<AccountFilter value={[]} onChange={() => {}} />);

    expect(await screen.findByTestId("account-filter-chip-1")).toBeInTheDocument();
    expect(screen.getByTestId("account-filter-chip-2")).toBeInTheDocument();
    expect(apiFetchMock).toHaveBeenCalledWith("/api/v1/accounts");
  });

  it("toggles a chip on and reports the new value via onChange", async () => {
    apiFetchMock.mockResolvedValueOnce(ACCOUNTS);
    const onChange = vi.fn();

    renderWithSWR(<AccountFilter value={[]} onChange={onChange} />);

    const chip = await screen.findByTestId("account-filter-chip-1");
    fireEvent.click(chip);
    expect(onChange).toHaveBeenCalledWith([1]);
  });

  it("toggles a chip off when its id is already selected", async () => {
    apiFetchMock.mockResolvedValueOnce(ACCOUNTS);
    const onChange = vi.fn();

    renderWithSWR(<AccountFilter value={[1, 2]} onChange={onChange} />);

    const chip = await screen.findByTestId("account-filter-chip-1");
    fireEvent.click(chip);
    expect(onChange).toHaveBeenCalledWith([2]);
  });

  it("excludes deactivated accounts from the chip list", async () => {
    apiFetchMock.mockResolvedValueOnce([
      {
        id: 10,
        name: "Checking",
        account_type_id: 1,
        account_type_name: "Bank",
        account_type_slug: "bank",
        balance: 0,
        currency: "USD",
        is_active: true,
        close_day: null,
        is_default: true,
      },
      {
        id: 11,
        name: "Old Savings",
        account_type_id: 1,
        account_type_name: "Bank",
        account_type_slug: "bank",
        balance: 0,
        currency: "USD",
        is_active: false,
        close_day: null,
        is_default: false,
      },
    ]);

    renderWithSWR(<AccountFilter value={[]} onChange={() => {}} />);

    expect(await screen.findByTestId("account-filter-chip-10")).toBeInTheDocument();
    expect(screen.getByText("Checking")).toBeInTheDocument();
    expect(screen.queryByTestId("account-filter-chip-11")).not.toBeInTheDocument();
    expect(screen.queryByText("Old Savings")).not.toBeInTheDocument();
  });

  // TBD-464: the transactions page lists every account, inactive ones
  // included, so it opts in. Reports keeps the default.
  it("shows inactive accounts only when includeInactive is set", async () => {
    const withInactive = [...ACCOUNTS, { ...ACCOUNTS[0], id: 3, name: "Old Savings", is_active: false }];
    apiFetchMock.mockResolvedValue(withInactive as never);

    const { unmount } = renderWithSWR(<AccountFilter value={[]} onChange={() => {}} />);
    await screen.findByTestId("account-filter-chip-1");
    expect(screen.queryByTestId("account-filter-chip-3")).toBeNull();
    unmount();

    renderWithSWR(<AccountFilter value={[]} onChange={() => {}} includeInactive />);
    expect(await screen.findByTestId("account-filter-chip-3")).toBeInTheDocument();
  });

  // TBD-464: selection is not colour-only (One Brass Rule restyle). A
  // pressed chip carries a Check icon; an unpressed one does not.
  it("renders a Check icon on pressed chips only, and exposes the chips as a named group", async () => {
    apiFetchMock.mockResolvedValueOnce(ACCOUNTS);

    renderWithSWR(<AccountFilter value={[1]} onChange={() => {}} />);

    const pressed = await screen.findByRole("button", { name: "Account Checking" });
    const unpressed = screen.getByRole("button", { name: "Account Credit Card" });
    expect(pressed).toHaveAttribute("aria-pressed", "true");
    expect(pressed.querySelector("svg.lucide-check")).not.toBeNull();
    expect(pressed.className.split(/\s+/)).not.toContain("bg-accent");
    expect(unpressed).toHaveAttribute("aria-pressed", "false");
    expect(unpressed.querySelector("svg.lucide-check")).toBeNull();
    expect(screen.getByRole("group", { name: "Accounts" })).toContainElement(pressed);
  });

  it("renders an error state when the fetch fails", async () => {
    apiFetchMock.mockRejectedValueOnce(new Error("boom"));

    renderWithSWR(<AccountFilter value={[]} onChange={() => {}} />);

    await waitFor(() =>
      expect(screen.getByTestId("account-filter-error")).toBeInTheDocument(),
    );
  });

  it("shares the bare-path accounts key (no duplicate ?for=reports-filter fetch)", async () => {
    // Mount the shared `useAccounts` hook alongside the filter in ONE SWR
    // cache. On the shared bare key both dedupe to a single request; the old
    // `?for=reports-filter` key would issue a second, duplicate fetch.
    apiFetchMock.mockResolvedValue(ACCOUNTS as never);

    function Harness() {
      useAccounts(true);
      return <AccountFilter value={[]} onChange={() => {}} />;
    }

    renderWithSWR(<Harness />);

    await screen.findByTestId("account-filter-chip-1");
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });

    const accountsCalls = apiFetchMock.mock.calls.filter(
      ([url]) => url === "/api/v1/accounts",
    );
    expect(accountsCalls).toHaveLength(1);
  });

  it("shows the loading skeleton (not the empty state) while auth is gated off", async () => {
    vi.mocked(useAuth).mockReturnValue({ user: null, loading: true } as never);
    apiFetchMock.mockResolvedValue(ACCOUNTS as never);

    renderWithSWR(<AccountFilter value={[]} onChange={() => {}} />);

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByTestId("account-filter-loading")).toBeInTheDocument();
    expect(screen.queryByText("No accounts yet")).not.toBeInTheDocument();
  });

  it("does not fetch while auth is still loading (auth gate)", async () => {
    vi.mocked(useAuth).mockReturnValue({ user: null, loading: true } as never);
    apiFetchMock.mockResolvedValue(ACCOUNTS as never);

    renderWithSWR(<AccountFilter value={[]} onChange={() => {}} />);

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(apiFetchMock).not.toHaveBeenCalled();
  });
});
