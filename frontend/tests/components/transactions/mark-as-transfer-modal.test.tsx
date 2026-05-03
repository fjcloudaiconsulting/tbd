import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import MarkAsTransferModal from "@/components/transactions/MarkAsTransferModal";
import { apiFetch } from "@/lib/api";
import type { Account, Transaction } from "@/lib/types";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

const source: Transaction = {
  id: 1,
  account_id: 10,
  account_name: "Checking",
  category_id: 100,
  category_name: "Other",
  description: "ATM",
  amount: 500,
  type: "expense",
  status: "settled",
  linked_transaction_id: null,
  recurring_id: null,
  date: "2026-04-29",
  settled_date: "2026-04-29",
  is_imported: false,
};

const accounts: Account[] = [
  {
    id: 10,
    name: "Checking",
    account_type_id: 1,
    account_type_name: "Bank",
    account_type_slug: "bank",
    balance: 1000,
    currency: "EUR",
    is_active: true,
    close_day: null,
    is_default: true,
  },
  {
    id: 20,
    name: "Savings",
    account_type_id: 1,
    account_type_name: "Bank",
    account_type_slug: "bank",
    balance: 500,
    currency: "EUR",
    is_active: true,
    close_day: null,
    is_default: false,
  },
  {
    id: 30,
    name: "USD Account",
    account_type_id: 1,
    account_type_name: "Bank",
    account_type_slug: "bank",
    balance: 0,
    currency: "USD",
    is_active: true,
    close_day: null,
    is_default: false,
  },
];

describe("MarkAsTransferModal", () => {
  const apiFetchMock = vi.mocked(apiFetch);

  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  it("Stage 1 filters AccountSelect to other accounts with same currency", () => {
    render(
      <MarkAsTransferModal
        source={source}
        accounts={accounts}
        onConverted={vi.fn()}
        onCancel={vi.fn()}
      />
    );
    const select = screen.getByRole("combobox");
    expect(select).toBeInTheDocument();
    const options = select.querySelectorAll("option");
    const optionValues = Array.from(options).map((o) => o.getAttribute("value"));
    expect(optionValues).toContain("20");
    expect(optionValues).not.toContain("10");
    expect(optionValues).not.toContain("30");
  });

  it("Stage 2 zero candidates: shows 'Create partner leg' as primary action", async () => {
    apiFetchMock.mockResolvedValueOnce({ candidates: [] } as never);
    render(
      <MarkAsTransferModal
        source={source}
        accounts={accounts}
        onConverted={vi.fn()}
        onCancel={vi.fn()}
      />
    );
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "20" } });
    await waitFor(() =>
      expect(screen.getByText(/No matching un-linked rows/i)).toBeInTheDocument()
    );
    expect(
      screen.getByRole("button", { name: /Create partner leg/i })
    ).toBeEnabled();
  });

  it("Stage 2 single same-day match: candidate pre-selected, primary 'Pair as transfer' enabled", async () => {
    apiFetchMock.mockResolvedValueOnce({
      candidates: [
        {
          id: 99,
          date: "2026-04-29",
          description: "Buffer received",
          amount: 500,
          account_id: 20,
          account_name: "Savings",
          date_diff_days: 0,
          confidence: "same_day",
        },
      ],
    } as never);
    render(
      <MarkAsTransferModal
        source={source}
        accounts={accounts}
        onConverted={vi.fn()}
        onCancel={vi.fn()}
      />
    );
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "20" } });
    await waitFor(() => {
      const radio = screen.getByRole("radio");
      expect(radio).toBeChecked();
    });
    expect(
      screen.getByRole("button", { name: /Pair as transfer/i })
    ).toBeEnabled();
  });

  it("Stage 2 single near-date match: radio NOT pre-selected, primary disabled until ticked", async () => {
    apiFetchMock.mockResolvedValueOnce({
      candidates: [
        {
          id: 99,
          date: "2026-04-30",
          description: "Buffer",
          amount: 500,
          account_id: 20,
          account_name: "Savings",
          date_diff_days: 1,
          confidence: "near_date",
        },
      ],
    } as never);
    render(
      <MarkAsTransferModal
        source={source}
        accounts={accounts}
        onConverted={vi.fn()}
        onCancel={vi.fn()}
      />
    );
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "20" } });
    await waitFor(() =>
      expect(screen.getByText(/Date differs by 1 day/i)).toBeInTheDocument()
    );
    const radio = screen.getByRole("radio");
    expect(radio).not.toBeChecked();
    expect(
      screen.getByRole("button", { name: /Create partner leg/i })
    ).toBeDisabled();
    fireEvent.click(radio);
    expect(
      screen.getByRole("button", { name: /Pair as transfer/i })
    ).toBeEnabled();
  });

  it("Stage 2 multi-candidate: radios shown, none pre-selected, primary disabled until pick", async () => {
    apiFetchMock.mockResolvedValueOnce({
      candidates: [
        {
          id: 99,
          date: "2026-04-29",
          description: "A",
          amount: 500,
          account_id: 20,
          account_name: "Savings",
          date_diff_days: 0,
          confidence: "same_day",
        },
        {
          id: 100,
          date: "2026-04-29",
          description: "B",
          amount: 500,
          account_id: 20,
          account_name: "Savings",
          date_diff_days: 0,
          confidence: "same_day",
        },
      ],
    } as never);
    render(
      <MarkAsTransferModal
        source={source}
        accounts={accounts}
        onConverted={vi.fn()}
        onCancel={vi.fn()}
      />
    );
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "20" } });
    await waitFor(() => expect(screen.getAllByRole("radio").length).toBe(2));
    const radios = screen.getAllByRole("radio");
    expect(radios.every((r) => !(r as HTMLInputElement).checked)).toBe(true);
    expect(
      screen.getByRole("button", { name: /Create partner leg/i })
    ).toBeDisabled();
    fireEvent.click(radios[0]);
    expect(
      screen.getByRole("button", { name: /Pair as transfer/i })
    ).toBeEnabled();
  });

  it("does not enable Create partner leg when candidate fetch fails", async () => {
    apiFetchMock.mockRejectedValueOnce(new Error("transient API error"));
    render(
      <MarkAsTransferModal
        source={source}
        accounts={accounts}
        onConverted={vi.fn()}
        onCancel={vi.fn()}
      />
    );
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "20" } });
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/transient API error/i)
    );
    // The "Create partner leg" button must NOT be enabled — fetch failed,
    // not "zero candidates".
    const createButton = screen.queryByRole("button", { name: /Create partner leg/i });
    if (createButton) {
      expect(createButton).toBeDisabled();
    }
    // Stronger assertion: the "no matching" notice shouldn't appear (only on
    // successful empty fetch).
    expect(screen.queryByText(/No matching un-linked rows/i)).not.toBeInTheDocument();
  });

  it("submit pair calls POST /convert-to-transfer with pair_with_transaction_id", async () => {
    apiFetchMock.mockResolvedValueOnce({
      candidates: [
        {
          id: 99,
          date: "2026-04-29",
          description: "X",
          amount: 500,
          account_id: 20,
          account_name: "Savings",
          date_diff_days: 0,
          confidence: "same_day",
        },
      ],
    } as never);
    apiFetchMock.mockResolvedValueOnce([] as never);
    const onConverted = vi.fn();
    render(
      <MarkAsTransferModal
        source={source}
        accounts={accounts}
        onConverted={onConverted}
        onCancel={vi.fn()}
      />
    );
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "20" } });
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /Pair as transfer/i })
      ).toBeEnabled()
    );
    fireEvent.click(screen.getByRole("button", { name: /Pair as transfer/i }));
    await waitFor(() => expect(onConverted).toHaveBeenCalled());
    expect(apiFetchMock).toHaveBeenLastCalledWith(
      "/api/v1/transactions/1/convert-to-transfer",
      expect.objectContaining({
        method: "POST",
        body: expect.stringContaining('"pair_with_transaction_id":99'),
      })
    );
  });
});
