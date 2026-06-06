import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import OverflowMenu, {
  type OverflowMenuItem,
} from "@/components/ui/OverflowMenu";

function items(onDelete = vi.fn(), onActivate = vi.fn()): OverflowMenuItem[] {
  return [
    { label: "Set default", onSelect: vi.fn() },
    { label: "Deactivate", onSelect: onActivate },
    { label: "Delete", onSelect: onDelete, danger: true },
  ];
}

describe("OverflowMenu", () => {
  it("renders a trigger with the menu-button aria contract", () => {
    render(<OverflowMenu items={items()} testId="row-overflow" />);
    const trigger = screen.getByTestId("row-overflow");
    expect(trigger).toHaveAttribute("aria-haspopup", "menu");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(trigger).toHaveAttribute("aria-label", "More actions");
  });

  it("honours a custom trigger label", () => {
    render(<OverflowMenu items={items()} label="More actions for Amex" />);
    expect(
      screen.getByRole("button", { name: "More actions for Amex" }),
    ).toBeInTheDocument();
  });

  it("renders nothing when there are no items", () => {
    const { container } = render(<OverflowMenu items={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("opens on click and renders each item, including the danger variant", async () => {
    render(<OverflowMenu items={items()} testId="row-overflow" />);
    expect(screen.queryByRole("menu")).toBeNull();

    fireEvent.click(screen.getByTestId("row-overflow"));

    await waitFor(() => expect(screen.getByRole("menu")).toBeInTheDocument());
    expect(screen.getByTestId("row-overflow")).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(screen.getByRole("menuitem", { name: "Set default" })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Deactivate" })).toBeInTheDocument();
    const del = screen.getByRole("menuitem", { name: "Delete" });
    expect(del).toBeInTheDocument();
    expect(del.className).toContain("text-danger");
  });

  it("fires the item's onSelect and closes the menu when clicked", async () => {
    const onDelete = vi.fn();
    render(<OverflowMenu items={items(onDelete)} testId="row-overflow" />);
    fireEvent.click(screen.getByTestId("row-overflow"));
    await waitFor(() =>
      expect(screen.getByRole("menuitem", { name: "Delete" })).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole("menuitem", { name: "Delete" }));
    expect(onDelete).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  });

  it("uses ariaLabel override for the accessible name when provided", async () => {
    render(
      <OverflowMenu
        items={[
          {
            label: "Set default",
            ariaLabel: "Set Amex as default",
            onSelect: vi.fn(),
          },
        ]}
        testId="row-overflow"
      />,
    );
    fireEvent.click(screen.getByTestId("row-overflow"));
    await waitFor(() =>
      expect(
        screen.getByRole("menuitem", { name: "Set Amex as default" }),
      ).toBeInTheDocument(),
    );
  });

  it("focuses the first item on open", async () => {
    render(<OverflowMenu items={items()} testId="row-overflow" />);
    fireEvent.click(screen.getByTestId("row-overflow"));
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByRole("menuitem", { name: "Set default" }),
      ),
    );
  });

  it("ArrowDown / ArrowUp cycle focus between items", async () => {
    render(<OverflowMenu items={items()} testId="row-overflow" />);
    fireEvent.click(screen.getByTestId("row-overflow"));
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByRole("menuitem", { name: "Set default" }),
      ),
    );

    fireEvent.keyDown(screen.getByRole("menu"), { key: "ArrowDown" });
    expect(document.activeElement).toBe(
      screen.getByRole("menuitem", { name: "Deactivate" }),
    );
    fireEvent.keyDown(screen.getByRole("menu"), { key: "ArrowUp" });
    expect(document.activeElement).toBe(
      screen.getByRole("menuitem", { name: "Set default" }),
    );
  });

  it("closes on Escape and returns focus to the trigger", async () => {
    render(<OverflowMenu items={items()} testId="row-overflow" />);
    fireEvent.click(screen.getByTestId("row-overflow"));
    await waitFor(() => expect(screen.getByRole("menu")).toBeInTheDocument());

    await act(async () => {
      fireEvent.keyDown(document, { key: "Escape" });
    });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    expect(document.activeElement).toBe(screen.getByTestId("row-overflow"));
  });

  it("closes on Tab and returns focus to the trigger", async () => {
    render(<OverflowMenu items={items()} testId="row-overflow" />);
    fireEvent.click(screen.getByTestId("row-overflow"));
    await waitFor(() => expect(screen.getByRole("menu")).toBeInTheDocument());

    await act(async () => {
      fireEvent.keyDown(screen.getByRole("menu"), { key: "Tab" });
    });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    expect(document.activeElement).toBe(screen.getByTestId("row-overflow"));
  });

  it("closes on outside mousedown", async () => {
    render(
      <div>
        <button type="button" data-testid="outside">
          outside
        </button>
        <OverflowMenu items={items()} testId="row-overflow" />
      </div>,
    );
    fireEvent.click(screen.getByTestId("row-overflow"));
    await waitFor(() => expect(screen.getByRole("menu")).toBeInTheDocument());

    fireEvent.mouseDown(screen.getByTestId("outside"));
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  });
});
