import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";

import AccountFilter from "@/components/reports/filters/AccountFilter";
import { apiFetch } from "@/lib/api";

vi.mock("@/lib/api", () => ({
  apiFetch: vi.fn(),
}));

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

function renderIsolated(ui: React.ReactElement) {
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      {ui}
    </SWRConfig>,
  );
}

describe("AccountFilter", () => {
  const apiFetchMock = vi.mocked(apiFetch);

  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  it("fetches accounts on mount and renders one chip per account", async () => {
    apiFetchMock.mockResolvedValueOnce(ACCOUNTS);

    renderIsolated(<AccountFilter value={[]} onChange={() => {}} />);

    expect(await screen.findByTestId("account-filter-chip-1")).toBeInTheDocument();
    expect(screen.getByTestId("account-filter-chip-2")).toBeInTheDocument();
    expect(apiFetchMock).toHaveBeenCalledWith("/api/v1/accounts");
  });

  it("toggles a chip on and reports the new value via onChange", async () => {
    apiFetchMock.mockResolvedValueOnce(ACCOUNTS);
    const onChange = vi.fn();

    renderIsolated(<AccountFilter value={[]} onChange={onChange} />);

    const chip = await screen.findByTestId("account-filter-chip-1");
    fireEvent.click(chip);
    expect(onChange).toHaveBeenCalledWith([1]);
  });

  it("toggles a chip off when its id is already selected", async () => {
    apiFetchMock.mockResolvedValueOnce(ACCOUNTS);
    const onChange = vi.fn();

    renderIsolated(<AccountFilter value={[1, 2]} onChange={onChange} />);

    const chip = await screen.findByTestId("account-filter-chip-1");
    fireEvent.click(chip);
    expect(onChange).toHaveBeenCalledWith([2]);
  });

  it("renders an error state when the fetch fails", async () => {
    apiFetchMock.mockRejectedValueOnce(new Error("boom"));

    renderIsolated(<AccountFilter value={[]} onChange={() => {}} />);

    await waitFor(() =>
      expect(screen.getByTestId("account-filter-error")).toBeInTheDocument(),
    );
  });
});
