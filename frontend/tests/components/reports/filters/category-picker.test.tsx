import { renderWithSWR, act, fireEvent, screen, waitFor, within } from "../../../utils/render-with-swr";

import CategoryPicker from "@/components/reports/filters/CategoryPicker";
import { useCategories } from "@/lib/hooks/use-categories";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import type { Category } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  apiFetch: vi.fn(),
}));

vi.mock("@/components/auth/AuthProvider", () => ({ useAuth: vi.fn() }));

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

// Food (10) holds transactions of its own; Transport (20) holds none.
const OWN_CATEGORIES: Category[] = CATEGORIES.map((c) =>
  c.id === 10 ? { ...c, transaction_count: 4 } : c,
);

describe("CategoryPicker", () => {
  const apiFetchMock = vi.mocked(apiFetch);

  beforeEach(() => {
    apiFetchMock.mockReset();
    vi.mocked(useAuth).mockReturnValue({ user: { id: 1 }, loading: false } as never);
  });

  it("fetches categories on mount and renders the master/sub tree", async () => {
    apiFetchMock.mockResolvedValueOnce(CATEGORIES);

    renderWithSWR(<CategoryPicker value={[]} onChange={() => {}} />);

    expect(await screen.findByTestId("category-master-10")).toBeInTheDocument();
    expect(screen.getByTestId("category-master-20")).toBeInTheDocument();
    expect(screen.getByTestId("category-sub-11")).toBeInTheDocument();
    expect(screen.getByTestId("category-sub-12")).toBeInTheDocument();
    expect(screen.getByTestId("category-sub-21")).toBeInTheDocument();
  });

  it("cascades the master selection into every sub", async () => {
    apiFetchMock.mockResolvedValueOnce(CATEGORIES);
    const onChange = vi.fn();

    renderWithSWR(<CategoryPicker value={[]} onChange={onChange} />);

    const masterFood = await screen.findByTestId("category-master-10");
    fireEvent.click(masterFood);

    // Master + both subs of Food should now be selected.
    expect(onChange).toHaveBeenCalledWith(expect.arrayContaining([10, 11, 12]));
    expect((onChange.mock.calls[0][0] as number[]).sort()).toEqual([10, 11, 12]);
  });

  it("leaves the master partial-checked when only one sub is unselected", async () => {
    apiFetchMock.mockResolvedValueOnce(CATEGORIES);

    renderWithSWR(
      <CategoryPicker value={[10, 11]} onChange={() => {}} />,
    );

    const master = await screen.findByTestId("category-master-10");
    await waitFor(() => {
      // ``indeterminate`` is a DOM-only flag. The component syncs it
      // via a ref; assert against the live element.
      expect((master as HTMLInputElement).indeterminate).toBe(true);
    });
  });

  // TBD-464. Reports keeps the linked master toggle: unchecking a fully
  // checked master clears its subs too, and there is never an (other) row.
  it("default mode: unchecking a checked master clears its subs, with no (other) row (Reports)", async () => {
    apiFetchMock.mockResolvedValueOnce(OWN_CATEGORIES);
    const onChange = vi.fn();

    renderWithSWR(<CategoryPicker value={[10, 11, 12]} onChange={onChange} />);

    fireEvent.click(await screen.findByRole("checkbox", { name: "Category Food" }));
    expect(onChange).toHaveBeenCalledWith([]);
    expect(screen.queryByRole("checkbox", { name: "Category Food (other)" })).toBeNull();
  });

  describe("ownRow (transactions panel, TBD-464 option C)", () => {
    function render(value: number[], onChange: (next: number[]) => void = () => {}) {
      apiFetchMock.mockResolvedValue(OWN_CATEGORIES as never);
      return renderWithSWR(<CategoryPicker ownRow value={value} onChange={onChange} />);
    }

    it("checking the group selects the master and all its subs", async () => {
      // FENCE. Kills: the master checking only itself.
      const onChange = vi.fn();
      render([21], onChange);

      fireEvent.click(await screen.findByRole("checkbox", { name: "Category Food" }));
      expect((onChange.mock.calls[0][0] as number[]).sort()).toEqual([10, 11, 12, 21]);
    });

    it("unchecking the group clears the master and all its subs", async () => {
      // FENCE. Kills: option B, where the master unchecks only itself.
      const onChange = vi.fn();
      render([10, 11, 12, 21], onChange);

      const food = (await screen.findByRole("checkbox", { name: "Category Food" })) as HTMLInputElement;
      expect(food).toBeChecked();
      fireEvent.click(food);
      expect(onChange).toHaveBeenCalledWith([21]);
    });

    it("a master with subs and own transactions has an (other) row that toggles only its id", async () => {
      const onChange = vi.fn();
      render([], onChange);

      const other = await screen.findByRole("checkbox", { name: "Category Food (other)" });
      fireEvent.click(other);
      expect(onChange).toHaveBeenCalledWith([10]);
    });

    it("the (other) row alone, or a missing sub, leaves the group indeterminate", async () => {
      const { unmount } = render([10]);
      let food = (await screen.findByRole("checkbox", { name: "Category Food" })) as HTMLInputElement;
      await waitFor(() => expect(food.indeterminate).toBe(true));
      expect(food).not.toBeChecked();
      expect(screen.getByRole("checkbox", { name: "Category Food (other)" })).toBeChecked();
      unmount();

      render([10, 11]);
      food = (await screen.findByRole("checkbox", { name: "Category Food" })) as HTMLInputElement;
      await waitFor(() => expect(food.indeterminate).toBe(true));
      expect(food).not.toBeChecked();
    });

    it("no (other) row without own transactions; the group toggle still adds and removes the master", async () => {
      // FENCE. Kills: the group toggle skipping the master when its (other)
      // row is hidden, which makes a fully checked group narrower than the
      // subtree.
      const onChange = vi.fn();
      const { unmount } = render([], onChange);

      await screen.findByRole("checkbox", { name: "Category Transport" });
      expect(screen.queryByRole("checkbox", { name: "Category Transport (other)" })).toBeNull();
      fireEvent.click(screen.getByRole("checkbox", { name: "Category Transport" }));
      expect((onChange.mock.calls[0][0] as number[]).sort()).toEqual([20, 21]);
      unmount();

      // Every VISIBLE row checked, hidden master not: the group reads checked,
      // and unchecking it still removes the master.
      const onChange2 = vi.fn();
      render([20, 21], onChange2);
      const transport = (await screen.findByRole("checkbox", { name: "Category Transport" })) as HTMLInputElement;
      expect(transport).toBeChecked();
      fireEvent.click(transport);
      expect(onChange2).toHaveBeenCalledWith([]);
    });

    it("a hidden master does not make the group partial", async () => {
      render([21]);
      const transport = (await screen.findByRole("checkbox", { name: "Category Transport" })) as HTMLInputElement;
      expect(transport).toBeChecked();
      expect(transport.indeterminate).toBe(false);
    });

    it("a search that hides a sub, every visible row checked: the group reads checked and clears everything", async () => {
      // FENCE. Kills: deriving the checked state from hidden rows, and a group
      // uncheck that leaves the hidden sub or the master behind.
      async function searchGroc(value: number[]) {
        const onChange = vi.fn();
        const view = render(value, onChange);
        await screen.findByRole("checkbox", { name: "Category Food" });
        fireEvent.change(screen.getByTestId("category-picker-search"), { target: { value: "groc" } });
        await waitFor(() => expect(screen.queryByRole("checkbox", { name: "Category Restaurants" })).toBeNull());
        return { onChange, view, food: screen.getByRole("checkbox", { name: "Category Food" }) as HTMLInputElement };
      }

      // The hidden sub is selected: the uncheck must clear it too.
      let run = await searchGroc([10, 11, 12, 21]);
      expect(run.food).toBeChecked();
      fireEvent.click(run.food);
      expect(run.onChange).toHaveBeenCalledWith([21]);
      run.view.unmount();

      // The hidden sub is NOT selected: the group still reads checked from
      // its visible rows, and the click still clears the whole group.
      run = await searchGroc([10, 11, 21]);
      expect(run.food).toBeChecked();
      fireEvent.click(run.food);
      expect(run.onChange).toHaveBeenCalledWith([21]);
    });

    it("a search that hides a sub: checking the group still includes it", async () => {
      // FENCE (re-review NB-5). Kills: building the group's ids from the
      // search-filtered node.
      const onChange = vi.fn();
      render([], onChange);

      await screen.findByRole("checkbox", { name: "Category Food" });
      fireEvent.change(screen.getByTestId("category-picker-search"), { target: { value: "groc" } });
      await waitFor(() => expect(screen.queryByRole("checkbox", { name: "Category Restaurants" })).toBeNull());

      fireEvent.click(screen.getByRole("checkbox", { name: "Category Food" }));
      expect((onChange.mock.calls[0][0] as number[]).sort()).toEqual([10, 11, 12]);
    });

    it("native checkboxes carry no aria-checked; the partial group sets indeterminate", async () => {
      // FENCE. Kills: aria-checked on a native checkbox (ARIA in HTML forbids
      // it), and a partial group with no indeterminate state.
      const onChange = vi.fn();
      render([11], onChange);
      const food = (await screen.findByRole("checkbox", { name: "Category Food" })) as HTMLInputElement;
      await waitFor(() => expect(food.indeterminate).toBe(true));
      for (const box of screen.getAllByRole("checkbox")) {
        expect(box).not.toHaveAttribute("aria-checked");
      }
      // Structural: the (other) row is a 44px touch target. Behavioural: the
      // whole label, not just the box, toggles it.
      const otherLabel = (screen.getByRole("checkbox", { name: "Category Food (other)" }) as HTMLElement).closest("label")!;
      expect(otherLabel.className).toContain("min-h-[44px]");
      fireEvent.click(within(otherLabel).getByText("Food (other)"));
      expect(onChange).toHaveBeenCalledWith([11, 10]);
    });
  });

  // TBD-464 visual gate: the tree scrolled horizontally in the 16rem side
  // panel. jsdom has no layout, so this fences the structure that prevents it
  // in both modes: the scroll box hides x-overflow, every name can shrink and
  // wrap (even a single long token), and no checkbox or count can be squashed.
  describe.each([
    ["Reports (default)", false],
    ["transactions panel (ownRow)", true],
  ])("never scrolls horizontally: %s", (_mode, ownRow) => {
    // A long master with its own transactions, so ownRow renders the widest
    // row there is: "Subscriptions & Streaming Services (other)", indented.
    const LONG: Category[] = [
      ...OWN_CATEGORIES,
      { ...CATEGORIES[0], id: 30, name: "Subscriptions & Streaming Services", slug: "subs", transaction_count: 2 },
      { ...CATEGORIES[1], id: 31, name: "SupercalifragilisticexpialidociousStreaming", parent_id: 30, parent_name: "Subscriptions & Streaming Services", slug: "long" },
    ];

    it("hides x-overflow, lets every name wrap, and keeps checkboxes and counts unsquashed", async () => {
      apiFetchMock.mockResolvedValue(LONG as never);
      renderWithSWR(<CategoryPicker ownRow={ownRow} value={[]} onChange={() => {}} />);

      await screen.findByRole("checkbox", { name: "Category Subscriptions & Streaming Services" });
      if (ownRow) {
        screen.getByRole("checkbox", { name: "Category Subscriptions & Streaming Services (other)" });
      }

      const tree = screen.getByRole("group", { name: "Categories" });
      expect(tree.className.split(/\s+/)).toContain("overflow-x-hidden");

      const boxes = within(tree).getAllByRole("checkbox");
      expect(boxes.length).toBeGreaterThan(0);
      for (const box of boxes) {
        expect(box.className.split(/\s+/)).toContain("shrink-0");
      }

      const labels = Array.from(tree.querySelectorAll("label"));
      expect(labels).toHaveLength(boxes.length);
      for (const label of labels) {
        const names = Array.from(label.querySelectorAll(":scope > span:not([data-testid^='category-count-'])"));
        expect(names).toHaveLength(1);
        const classes = names[0].className.split(/\s+/);
        expect(classes).toContain("min-w-0");
        expect(classes).toContain("[overflow-wrap:anywhere]");
      }
      for (const count of Array.from(tree.querySelectorAll("[data-testid^='category-count-']"))) {
        expect(count.className.split(/\s+/)).toContain("shrink-0");
      }
    });
  });

  it("filters the tree by the search input", async () => {
    apiFetchMock.mockResolvedValueOnce(CATEGORIES);

    renderWithSWR(<CategoryPicker value={[]} onChange={() => {}} />);

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

    renderWithSWR(<CategoryPicker value={[]} onChange={() => {}} />);

    await waitFor(() =>
      expect(screen.getByTestId("category-picker-error")).toBeInTheDocument(),
    );
  });

  it("shares the bare-path categories key (no duplicate ?for=reports-filter fetch)", async () => {
    apiFetchMock.mockResolvedValue(CATEGORIES as never);

    function Harness() {
      useCategories(true);
      return <CategoryPicker value={[]} onChange={() => {}} />;
    }

    renderWithSWR(<Harness />);

    await screen.findByTestId("category-master-10");
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });

    const categoriesCalls = apiFetchMock.mock.calls.filter(
      ([url]) => url === "/api/v1/categories",
    );
    expect(categoriesCalls).toHaveLength(1);
  });

  it("shows the loading skeleton (not the empty state) while auth is gated off", async () => {
    vi.mocked(useAuth).mockReturnValue({ user: null, loading: true } as never);
    apiFetchMock.mockResolvedValue(CATEGORIES as never);

    renderWithSWR(<CategoryPicker value={[]} onChange={() => {}} />);

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByTestId("category-picker-loading")).toBeInTheDocument();
    expect(screen.queryByText("No categories yet")).not.toBeInTheDocument();
  });

  it("does not fetch while auth is still loading (auth gate)", async () => {
    vi.mocked(useAuth).mockReturnValue({ user: null, loading: true } as never);
    apiFetchMock.mockResolvedValue(CATEGORIES as never);

    renderWithSWR(<CategoryPicker value={[]} onChange={() => {}} />);

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(apiFetchMock).not.toHaveBeenCalled();
  });
});
