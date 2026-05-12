import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import Tooltip from "@/components/Tooltip";

describe("Tooltip", () => {
  it("renders the default trigger button and wires aria-describedby on open", async () => {
    render(<Tooltip content="Hello world" />);

    const trigger = screen.getByTestId("tooltip-trigger");
    expect(trigger).toBeInTheDocument();
    expect(trigger).not.toHaveAttribute("aria-describedby");
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();

    act(() => {
      fireEvent.focus(trigger);
    });

    const bubble = await screen.findByRole("tooltip");
    expect(bubble).toHaveTextContent("Hello world");
    expect(trigger).toHaveAttribute("aria-describedby", bubble.id);
  });

  it("dismisses on Escape and returns focus to the trigger", async () => {
    render(<Tooltip content="Dismiss me" />);
    const trigger = screen.getByTestId("tooltip-trigger");

    act(() => {
      trigger.focus();
      fireEvent.focus(trigger);
    });

    await screen.findByRole("tooltip");

    act(() => {
      fireEvent.keyDown(document, { key: "Escape" });
    });

    await waitFor(() => {
      expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    });
    expect(document.activeElement).toBe(trigger);
  });

  it("toggles open and closed on click", async () => {
    render(<Tooltip content="Tap me" />);
    const trigger = screen.getByTestId("tooltip-trigger");

    act(() => {
      fireEvent.click(trigger);
    });
    await screen.findByRole("tooltip");

    act(() => {
      fireEvent.click(trigger);
    });
    await waitFor(() => {
      expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    });
  });

  it("stays open when a mouse click fires focus then click in the same tick (browser order)", async () => {
    // Real browsers fire `focus` immediately before `click` when a user
    // mouse-clicks an unfocused element. The naive toggle-on-click made
    // the focus-open immediately toggle closed, inverting clicks. This
    // test guards against that regression.
    render(<Tooltip content="Browser order" />);
    const trigger = screen.getByTestId("tooltip-trigger");

    // First gesture: focus fires, then click fires in the same tick.
    act(() => {
      fireEvent.focus(trigger);
      fireEvent.click(trigger);
    });
    expect(await screen.findByRole("tooltip")).toBeInTheDocument();

    // Second click (trigger already focused): now this should toggle
    // closed since it is a deliberate click without a fresh focus.
    act(() => {
      fireEvent.click(trigger);
    });
    await waitFor(() => {
      expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    });

    // Third click: focus is still on the trigger from the second click,
    // so this is a standalone click and should reopen.
    act(() => {
      fireEvent.click(trigger);
    });
    expect(await screen.findByRole("tooltip")).toBeInTheDocument();
  });

  it("renders a Learn more link to /docs#<section> when learnMoreSection is provided", async () => {
    render(
      <Tooltip
        content="With docs link"
        learnMoreSection="transactions"
      />,
    );

    act(() => {
      fireEvent.focus(screen.getByTestId("tooltip-trigger"));
    });

    const link = await screen.findByTestId("tooltip-learn-more");
    expect(link).toHaveAttribute("href", "/docs#transactions");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(link).toHaveAttribute("data-section", "transactions");
  });

  it("omits Learn more when no learnMoreSection is provided", async () => {
    render(<Tooltip content="No link" />);

    act(() => {
      fireEvent.focus(screen.getByTestId("tooltip-trigger"));
    });

    await screen.findByRole("tooltip");
    expect(screen.queryByTestId("tooltip-learn-more")).not.toBeInTheDocument();
  });

  it("respects prefers-reduced-motion by skipping the transition class", async () => {
    const original = window.matchMedia;
    window.matchMedia = ((query: string) => ({
      matches: query.includes("prefers-reduced-motion"),
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    })) as unknown as typeof window.matchMedia;

    try {
      render(<Tooltip content="No motion" />);

      act(() => {
        fireEvent.focus(screen.getByTestId("tooltip-trigger"));
      });

      const bubble = await screen.findByRole("tooltip");
      expect(bubble).toHaveAttribute("data-reduced-motion", "true");
      expect(bubble.className).not.toMatch(/transition-opacity/);
    } finally {
      window.matchMedia = original;
    }
  });

  it("accepts a custom trigger element and wires events through cloning", async () => {
    const { unmount } = render(
      <Tooltip
        content="Custom trigger content"
        trigger={
          <button type="button" data-testid="custom-trigger">
            Help
          </button>
        }
      />,
    );

    const custom = screen.getByTestId("custom-trigger");
    expect(custom).toBeInTheDocument();
    expect(custom).not.toHaveAttribute("aria-describedby");

    act(() => {
      fireEvent.focus(custom);
    });

    const bubble = await screen.findByRole("tooltip");
    expect(bubble).toHaveTextContent("Custom trigger content");
    expect(custom).toHaveAttribute("aria-describedby", bubble.id);

    unmount();
  });

  it("renders into a portal (outside the trigger's parent)", async () => {
    const { container } = render(
      <div data-testid="parent-wrapper">
        <Tooltip content="Portal me" />
      </div>,
    );

    act(() => {
      fireEvent.focus(screen.getByTestId("tooltip-trigger"));
    });

    const bubble = await screen.findByRole("tooltip");
    const parent = container.querySelector("[data-testid='parent-wrapper']");
    expect(parent).not.toBeNull();
    expect(parent!.contains(bubble)).toBe(false);
    expect(document.body.contains(bubble)).toBe(true);
  });
});
