import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import AddTransactionFab from "@/components/floating/AddTransactionFab";
import { apiFetch } from "@/lib/api";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

const ACCT = {
  id: 1,
  name: "Checking",
  account_type_id: 1,
  account_type_name: "Checking",
  account_type_slug: "checking",
  balance: 1000,
  currency: "EUR",
  is_active: true,
  close_day: null,
  is_default: true,
};

const CAT = {
  id: 10,
  name: "Groceries",
  type: "expense" as const,
  parent_id: null,
  parent_name: null,
  description: null,
  slug: "groceries",
  is_system: false,
  transaction_count: 0,
};

function setupRefs() {
  const apiFetchMock = vi.mocked(apiFetch);
  apiFetchMock.mockReset();
  apiFetchMock.mockImplementation(async (url: string) => {
    if (url.startsWith("/api/v1/accounts")) return [ACCT] as never;
    if (url.startsWith("/api/v1/categories")) return [CAT] as never;
    return null as never;
  });
  return apiFetchMock;
}

describe("AddTransactionFab", () => {
  it("renders the floating button with an accessible label", async () => {
    setupRefs();
    await act(async () => {
      render(<AddTransactionFab />);
    });
    const fab = screen.getByTestId("add-transaction-fab");
    expect(fab).toBeInTheDocument();
    expect(fab).toHaveAttribute("aria-label", "Add transaction");
  });

  it("is anchored inside the AnchorZone (bottom-right cluster)", async () => {
    setupRefs();
    await act(async () => {
      render(<AddTransactionFab />);
    });
    const zone = screen.getByTestId("anchor-zone");
    expect(zone).toContainElement(screen.getByTestId("add-transaction-fab"));
  });

  it("opens the panel on click", async () => {
    setupRefs();
    await act(async () => {
      render(<AddTransactionFab />);
    });
    expect(screen.queryByTestId("add-transaction-panel")).toBeNull();
    fireEvent.click(screen.getByTestId("add-transaction-fab"));
    await waitFor(() => {
      expect(screen.getByTestId("add-transaction-panel")).toBeInTheDocument();
    });
    // The panel header surfaces "Add transaction" — distinct from the
    // button's aria-label.
    expect(screen.getByRole("dialog")).toHaveTextContent("Add transaction");
  });

  it("renders the transaction form inside the open panel", async () => {
    setupRefs();
    await act(async () => {
      render(<AddTransactionFab />);
    });
    fireEvent.click(screen.getByTestId("add-transaction-fab"));
    await waitFor(() => {
      expect(screen.getByLabelText("Description")).toBeInTheDocument();
    });
    expect(screen.getByLabelText("Amount")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Save$/i })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /save and add new/i }),
    ).toBeInTheDocument();
  });
});
