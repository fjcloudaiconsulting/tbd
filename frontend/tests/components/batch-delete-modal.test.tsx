import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import BatchDeleteModal from "@/components/categories/BatchDeleteModal";
import { apiFetch } from "@/lib/api";
import type { Category } from "@/lib/types";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

const cats: Category[] = [
  // Masters
  { id: 100, name: "Food", type: "expense", parent_id: null, parent_name: null, description: null, slug: "food_dining", is_system: true, transaction_count: 0 },
  { id: 200, name: "Lifestyle", type: "expense", parent_id: null, parent_name: null, description: null, slug: "lifestyle", is_system: true, transaction_count: 0 },
  { id: 300, name: "Income", type: "income", parent_id: null, parent_name: null, description: null, slug: "income", is_system: true, transaction_count: 0 },
  { id: 500, name: "Mixed", type: "both", parent_id: null, parent_name: null, description: null, slug: null, is_system: false, transaction_count: 0 },
  // Subs
  { id: 101, name: "Restaurants", type: "expense", parent_id: 100, parent_name: "Food", description: null, slug: null, is_system: false, transaction_count: 5 },
  { id: 102, name: "Groceries", type: "expense", parent_id: 100, parent_name: "Food", description: null, slug: null, is_system: false, transaction_count: 0 },
  { id: 301, name: "Salary", type: "income", parent_id: 300, parent_name: "Income", description: null, slug: null, is_system: false, transaction_count: 0 },
];

describe("BatchDeleteModal target picker", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
  });

  it("renders the migration target picker for every selected sub, even with zero transactions", async () => {
    render(
      <BatchDeleteModal
        open
        selectedIds={[101, 102]}
        categories={cats}
        onCancel={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    // Both rows must have a picker (102 has transaction_count = 0).
    expect(await screen.findByTestId("batch-delete-target-101")).toBeInTheDocument();
    expect(screen.getByTestId("batch-delete-target-102")).toBeInTheDocument();

    const confirm = screen.getByTestId("batch-delete-confirm");
    expect(confirm).toBeDisabled();
  });

  it("type-compat filter: expense source picker excludes income masters but includes BOTH", async () => {
    render(
      <BatchDeleteModal
        open
        selectedIds={[102]}
        categories={cats}
        onCancel={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    const select = (await screen.findByTestId("batch-delete-target-102")) as HTMLSelectElement;
    const optionValues = Array.from(select.options).map((o) => o.value).filter(Boolean);
    expect(optionValues).toContain("200"); // Lifestyle (expense)
    expect(optionValues).toContain("500"); // Mixed (both)
    // Source itself is excluded.
    expect(optionValues).not.toContain("102");
    // Income master is excluded.
    expect(optionValues).not.toContain("300");
  });

  it("type-compat filter: income source picker excludes expense masters but includes BOTH", async () => {
    render(
      <BatchDeleteModal
        open
        selectedIds={[301]}
        categories={cats}
        onCancel={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    const select = (await screen.findByTestId("batch-delete-target-301")) as HTMLSelectElement;
    const optionValues = Array.from(select.options).map((o) => o.value).filter(Boolean);
    expect(optionValues).toContain("300"); // Income
    expect(optionValues).toContain("500"); // Mixed
    expect(optionValues).not.toContain("100"); // Food (expense)
    expect(optionValues).not.toContain("200"); // Lifestyle (expense)
  });

  it("submit is blocked until every row has a target picked", async () => {
    vi.mocked(apiFetch).mockResolvedValue(undefined);

    render(
      <BatchDeleteModal
        open
        selectedIds={[101, 102]}
        categories={cats}
        onCancel={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    const confirm = await screen.findByTestId("batch-delete-confirm");
    expect(confirm).toBeDisabled();

    fireEvent.change(screen.getByTestId("batch-delete-target-101"), { target: { value: "200" } });
    expect(confirm).toBeDisabled();

    fireEvent.change(screen.getByTestId("batch-delete-target-102"), { target: { value: "200" } });
    expect(confirm).not.toBeDisabled();
  });
});

describe("BatchDeleteModal async onSuccess", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
  });

  it("awaits async onSuccess and surfaces refresh errors with a Retry button", async () => {
    vi.mocked(apiFetch).mockResolvedValue(undefined);

    let invocations = 0;
    const onSuccess = vi.fn(async () => {
      invocations += 1;
      if (invocations === 1) throw new Error("reload failed");
    });

    render(
      <BatchDeleteModal
        open
        selectedIds={[102]}
        categories={cats}
        onCancel={vi.fn()}
        onSuccess={onSuccess}
      />,
    );

    fireEvent.change(await screen.findByTestId("batch-delete-target-102"), {
      target: { value: "200" },
    });
    fireEvent.click(screen.getByTestId("batch-delete-confirm"));

    const banner = await screen.findByTestId("batch-delete-refresh-error");
    expect(banner.textContent).toMatch(/reload failed/);

    fireEvent.click(screen.getByTestId("batch-delete-refresh-retry"));
    await waitFor(() => {
      expect(screen.queryByTestId("batch-delete-refresh-error")).not.toBeInTheDocument();
    });
    expect(invocations).toBe(2);
  });
});
