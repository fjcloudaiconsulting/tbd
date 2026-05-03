import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import UnpairTransferModal from "@/components/transactions/UnpairTransferModal";
import { apiFetch } from "@/lib/api";
import type { Category, Transaction } from "@/lib/types";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

// Mock CategorySelect with a native <select> so fireEvent.change can drive it
// in tests. The real CategorySelect is an autocomplete combobox; the unit
// test here only cares that the modal wires `value`, `onChange`, and
// `typeFilter` correctly per leg.
vi.mock("@/components/ui/CategorySelect", () => ({
  default: ({
    categories,
    typeFilter,
    value,
    onChange,
    "aria-label": ariaLabel,
  }: {
    categories: Category[];
    typeFilter?: "INCOME" | "EXPENSE";
    value: number | "";
    onChange: (id: number | "") => void;
    "aria-label"?: string;
  }) => {
    const visible = categories.filter((c) => {
      if (!typeFilter) return true;
      const t = typeFilter.toLowerCase();
      return c.type === t || c.type === "both";
    });
    return (
      <select
        role="combobox"
        aria-label={ariaLabel}
        data-type-filter={typeFilter ?? ""}
        value={value === "" ? "" : String(value)}
        onChange={(e) =>
          onChange(e.target.value === "" ? "" : Number(e.target.value))
        }
      >
        <option value="">Select...</option>
        {visible.map((c) => (
          <option key={c.id} value={c.id}>
            {c.name}
          </option>
        ))}
      </select>
    );
  },
}));

const expenseLeg: Transaction = {
  id: 1,
  account_id: 10,
  account_name: "Checking",
  category_id: 100,
  category_name: "Transfer",
  description: "Transfer",
  amount: 500,
  type: "expense",
  status: "settled",
  linked_transaction_id: 2,
  recurring_id: null,
  date: "2026-04-28",
  settled_date: "2026-04-28",
  is_imported: false,
};

const incomeLeg: Transaction = {
  id: 2,
  account_id: 20,
  account_name: "Savings",
  category_id: 100,
  category_name: "Transfer",
  description: "Transfer",
  amount: 500,
  type: "income",
  status: "settled",
  linked_transaction_id: 1,
  recurring_id: null,
  date: "2026-04-30",
  settled_date: "2026-04-30",
  is_imported: false,
};

const categories: Category[] = [
  {
    id: 11,
    name: "Groceries",
    type: "expense",
    parent_id: null,
    parent_name: null,
    description: null,
    slug: "groceries",
    is_system: false,
    transaction_count: 0,
  },
  {
    id: 12,
    name: "Salary",
    type: "income",
    parent_id: null,
    parent_name: null,
    description: null,
    slug: "salary",
    is_system: false,
    transaction_count: 0,
  },
  {
    id: 13,
    name: "Other",
    type: "both",
    parent_id: null,
    parent_name: null,
    description: null,
    slug: "other",
    is_system: true,
    transaction_count: 0,
  },
];

describe("UnpairTransferModal", () => {
  const apiFetchMock = vi.mocked(apiFetch);

  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  it("renders both legs and two type-filtered CategorySelects", () => {
    render(
      <UnpairTransferModal
        expenseLeg={expenseLeg}
        incomeLeg={incomeLeg}
        categories={categories}
        onUnpaired={vi.fn()}
        onCancel={vi.fn()}
      />
    );
    expect(screen.getByText(/Checking/)).toBeInTheDocument();
    expect(screen.getByText(/Savings/)).toBeInTheDocument();
    const selects = screen.getAllByRole("combobox") as HTMLSelectElement[];
    expect(selects.length).toBe(2);
    expect(selects[0].getAttribute("data-type-filter")).toBe("EXPENSE");
    expect(selects[1].getAttribute("data-type-filter")).toBe("INCOME");
  });

  it("submit disabled until both fallback categories selected", () => {
    render(
      <UnpairTransferModal
        expenseLeg={expenseLeg}
        incomeLeg={incomeLeg}
        categories={categories}
        onUnpaired={vi.fn()}
        onCancel={vi.fn()}
      />
    );
    const button = screen.getByRole("button", { name: /Unlink transfer/i });
    expect(button).toBeDisabled();

    const selects = screen.getAllByRole("combobox");
    fireEvent.change(selects[0], { target: { value: "11" } }); // expense → Groceries
    expect(button).toBeDisabled();

    fireEvent.change(selects[1], { target: { value: "12" } }); // income → Salary
    expect(button).toBeEnabled();
  });

  it("submit calls POST /unpair with both fallback ids", async () => {
    apiFetchMock.mockResolvedValueOnce([] as never);
    const onUnpaired = vi.fn();

    render(
      <UnpairTransferModal
        expenseLeg={expenseLeg}
        incomeLeg={incomeLeg}
        categories={categories}
        onUnpaired={onUnpaired}
        onCancel={vi.fn()}
      />
    );

    const selects = screen.getAllByRole("combobox");
    fireEvent.change(selects[0], { target: { value: "11" } });
    fireEvent.change(selects[1], { target: { value: "12" } });
    fireEvent.click(screen.getByRole("button", { name: /Unlink transfer/i }));

    await waitFor(() => expect(onUnpaired).toHaveBeenCalled());
    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/v1/transactions/1/unpair",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          expense_fallback_category_id: 11,
          income_fallback_category_id: 12,
        }),
      })
    );
  });
});
