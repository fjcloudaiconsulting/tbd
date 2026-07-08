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
    expect(
      apiFetchMock.mock.calls.some(
        ([url]) =>
          typeof url === "string" && url.includes("for=reports-filter"),
      ),
    ).toBe(false);
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
