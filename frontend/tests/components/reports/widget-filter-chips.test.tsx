/**
 * WidgetFilterChips — the per-widget filter-chip header row.
 *
 * In edit mode (``interactive``) each chip is a button that selects the
 * widget and opens the popover's Filters tab via ``onSelectFilters``. In
 * view mode the chips render as inert, non-focusable informational spans.
 */
import { fireEvent, render, screen } from "@testing-library/react";

import WidgetFilterChips from "@/components/reports/WidgetFilterChips";
import { buildPresetRanges } from "@/lib/reports/date-presets";
import type { BarWidget, WidgetFilters } from "@/lib/reports/types";
import type { Account, Category } from "@/lib/types";

const NOW = new Date(2026, 5, 15); // 2026-06-15 (stable)

function barWith(filters: WidgetFilters): BarWidget {
  return {
    id: "w_bar",
    type: "bar",
    title: "Bar",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      dimensions: ["category"],
      filters,
    },
  };
}

const ACCTS: Account[] = [
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
    name: "Savings",
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

const CATS: Category[] = [
  {
    id: 10,
    name: "Groceries",
    type: "expense",
    parent_id: null,
    parent_name: null,
    description: null,
    slug: "groceries",
    is_system: false,
    transaction_count: 0,
  },
];

describe("WidgetFilterChips (interactive / edit mode)", () => {
  it("renders a chip button per set filter and fires onSelectFilters on click", () => {
    const onSelectFilters = vi.fn();
    render(
      <WidgetFilterChips
        widget={barWith({ txn_type: ["expense"] })}
        canvasFilters={{}}
        accounts={[]}
        categories={[]}
        interactive
        onSelectFilters={onSelectFilters}
      />,
    );
    const chip = screen.getByTestId("widget-filter-chip-txn_type");
    expect(chip.tagName).toBe("BUTTON");
    fireEvent.click(chip);
    expect(onSelectFilters).toHaveBeenCalledOnce();
  });

  it("uses the HUMAN display label in the aria-label, not the raw key", () => {
    render(
      <WidgetFilterChips
        widget={barWith({ txn_type: ["expense"] })}
        canvasFilters={{}}
        accounts={[]}
        categories={[]}
        interactive
        onSelectFilters={() => {}}
      />,
    );
    // Edit verb + human label ("Expense"), never "Edit txn_type filter".
    expect(
      screen.getByRole("button", { name: "Edit Expense filter" }),
    ).toBeInTheDocument();
  });

  it("renders nothing when the widget has no set filters", () => {
    const { container } = render(
      <WidgetFilterChips
        widget={barWith({})}
        canvasFilters={{}}
        accounts={[]}
        categories={[]}
        interactive
        onSelectFilters={() => {}}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders a date chip and applies the overridden accent class when the widget date differs from canvas", () => {
    const presets = buildPresetRanges(NOW);
    render(
      <WidgetFilterChips
        widget={barWith({ date_range: { start: "2026-03-01", end: "2026-03-31" } })}
        canvasFilters={{ date_range: presets.this_month }}
        accounts={[]}
        categories={[]}
        interactive
        onSelectFilters={() => {}}
      />,
    );
    const chip = screen.getByTestId("widget-filter-chip-date");
    // Overridden date chips use the accent register.
    expect(chip.className).toContain("text-accent");
    expect(chip.className).not.toContain("text-text-secondary");
  });

  it("renders an inherited (non-overridden) date chip in the neutral register", () => {
    const presets = buildPresetRanges(NOW);
    render(
      <WidgetFilterChips
        widget={barWith({})}
        canvasFilters={{ date_range: presets.this_month }}
        accounts={[]}
        categories={[]}
        interactive
        onSelectFilters={() => {}}
      />,
    );
    const chip = screen.getByTestId("widget-filter-chip-date");
    expect(chip.className).toContain("text-text-secondary");
    expect(chip.className).not.toContain("text-accent");
  });

  it("shows the resolved name plus a +N that counts unresolved ids too (partial resolution)", () => {
    // id 1 resolves to "Checking"; id 99 does not — but the widget filters
    // on 2 accounts, so the count must be "+1", not bare "Checking".
    render(
      <WidgetFilterChips
        widget={barWith({ account_ids: [1, 99] })}
        canvasFilters={{}}
        accounts={ACCTS}
        categories={[]}
        interactive
        onSelectFilters={() => {}}
      />,
    );
    expect(screen.getByTestId("widget-filter-chip-accounts")).toHaveTextContent(
      "Checking +1",
    );
  });

  it("uses the singular count noun for a single unresolved id", () => {
    render(
      <WidgetFilterChips
        widget={barWith({ account_ids: [99] })}
        canvasFilters={{}}
        accounts={ACCTS}
        categories={[]}
        interactive
        onSelectFilters={() => {}}
      />,
    );
    expect(screen.getByTestId("widget-filter-chip-accounts")).toHaveTextContent(
      "1 account",
    );
  });

  it("uses the plural count noun for multiple unresolved ids", () => {
    render(
      <WidgetFilterChips
        widget={barWith({ category_ids: [98, 99] })}
        canvasFilters={{}}
        accounts={[]}
        categories={CATS}
        interactive
        onSelectFilters={() => {}}
      />,
    );
    expect(
      screen.getByTestId("widget-filter-chip-categories"),
    ).toHaveTextContent("2 categories");
  });
});

describe("WidgetFilterChips (non-interactive / view mode)", () => {
  it("renders chips as inert, non-focusable spans (no button, no Edit affordance)", () => {
    const onSelectFilters = vi.fn();
    render(
      <WidgetFilterChips
        widget={barWith({ txn_type: ["expense"] })}
        canvasFilters={{}}
        accounts={[]}
        categories={[]}
        interactive={false}
        onSelectFilters={onSelectFilters}
      />,
    );
    const chip = screen.getByTestId("widget-filter-chip-txn_type");
    // A span, not a button → not focusable, no false "Edit" affordance.
    expect(chip.tagName).toBe("SPAN");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Edit .* filter/)).not.toBeInTheDocument();
    // Clicking it does nothing (no onClick handler wired).
    fireEvent.click(chip);
    expect(onSelectFilters).not.toHaveBeenCalled();
  });
});
