import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";

import CategoryPicker from "@/components/reports/filters/CategoryPicker";
import { apiFetch } from "@/lib/api";
import type { Category } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  apiFetch: vi.fn(),
}));

const CATEGORIES: Category[] = [
  {
    id: 10,
    name: "Food",
    type: "expense",
    parent_id: null,
    parent_name: null,
    description: null,
    slug: "food",
    is_system: false,
    transaction_count: 0,
  },
  {
    id: 11,
    name: "Groceries",
    type: "expense",
    parent_id: 10,
    parent_name: "Food",
    description: null,
    slug: "groceries",
    is_system: false,
    transaction_count: 0,
  },
  {
    id: 12,
    name: "Restaurants",
    type: "expense",
    parent_id: 10,
    parent_name: "Food",
    description: null,
    slug: "restaurants",
    is_system: false,
    transaction_count: 0,
  },
  {
    id: 20,
    name: "Transport",
    type: "expense",
    parent_id: null,
    parent_name: null,
    description: null,
    slug: "transport",
    is_system: false,
    transaction_count: 0,
  },
  {
    id: 21,
    name: "Fuel",
    type: "expense",
    parent_id: 20,
    parent_name: "Transport",
    description: null,
    slug: "fuel",
    is_system: false,
    transaction_count: 0,
  },
];

function renderIsolated(ui: React.ReactElement) {
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      {ui}
    </SWRConfig>,
  );
}

describe("CategoryPicker", () => {
  const apiFetchMock = vi.mocked(apiFetch);

  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  it("fetches categories on mount and renders the master/sub tree", async () => {
    apiFetchMock.mockResolvedValueOnce(CATEGORIES);

    renderIsolated(<CategoryPicker value={[]} onChange={() => {}} />);

    expect(await screen.findByTestId("category-master-10")).toBeInTheDocument();
    expect(screen.getByTestId("category-master-20")).toBeInTheDocument();
    expect(screen.getByTestId("category-sub-11")).toBeInTheDocument();
    expect(screen.getByTestId("category-sub-12")).toBeInTheDocument();
    expect(screen.getByTestId("category-sub-21")).toBeInTheDocument();
  });

  it("cascades the master selection into every sub", async () => {
    apiFetchMock.mockResolvedValueOnce(CATEGORIES);
    const onChange = vi.fn();

    renderIsolated(<CategoryPicker value={[]} onChange={onChange} />);

    const masterFood = await screen.findByTestId("category-master-10");
    fireEvent.click(masterFood);

    // Master + both subs of Food should now be selected.
    expect(onChange).toHaveBeenCalledWith(expect.arrayContaining([10, 11, 12]));
    expect((onChange.mock.calls[0][0] as number[]).sort()).toEqual([10, 11, 12]);
  });

  it("leaves the master partial-checked when only one sub is unselected", async () => {
    apiFetchMock.mockResolvedValueOnce(CATEGORIES);

    renderIsolated(
      <CategoryPicker value={[10, 11]} onChange={() => {}} />,
    );

    const master = await screen.findByTestId("category-master-10");
    await waitFor(() => {
      // ``indeterminate`` is a DOM-only flag. The component syncs it
      // via a ref; assert against the live element.
      expect((master as HTMLInputElement).indeterminate).toBe(true);
    });
  });

  it("filters the tree by the search input", async () => {
    apiFetchMock.mockResolvedValueOnce(CATEGORIES);

    renderIsolated(<CategoryPicker value={[]} onChange={() => {}} />);

    await screen.findByTestId("category-master-10");
    fireEvent.change(screen.getByTestId("category-picker-search"), {
      target: { value: "fuel" },
    });

    await waitFor(() => {
      // The Food tree disappears since neither it nor its subs match
      // "fuel"; Transport stays because its sub "Fuel" matches.
      expect(screen.queryByTestId("category-master-10")).toBeNull();
      expect(screen.getByTestId("category-master-20")).toBeInTheDocument();
      expect(screen.getByTestId("category-sub-21")).toBeInTheDocument();
    });
  });

  it("renders an error state when the fetch fails", async () => {
    apiFetchMock.mockRejectedValueOnce(new Error("boom"));

    renderIsolated(<CategoryPicker value={[]} onChange={() => {}} />);

    await waitFor(() =>
      expect(screen.getByTestId("category-picker-error")).toBeInTheDocument(),
    );
  });
});
