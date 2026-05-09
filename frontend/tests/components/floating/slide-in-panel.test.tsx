import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";

import SlideInPanel from "@/components/floating/SlideInPanel";

function Harness({ initialOpen = false }: { initialOpen?: boolean }) {
  const [open, setOpen] = useState(initialOpen);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        open trigger
      </button>
      <SlideInPanel open={open} onClose={() => setOpen(false)} title="Test panel">
        <button type="button">first</button>
        <input aria-label="middle" />
        <button type="button">last</button>
      </SlideInPanel>
    </>
  );
}

describe("SlideInPanel", () => {
  it("renders nothing when closed", () => {
    render(<Harness initialOpen={false} />);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("opens when toggled", () => {
    render(<Harness initialOpen={false} />);
    fireEvent.click(screen.getByRole("button", { name: "open trigger" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText("Test panel")).toBeInTheDocument();
  });

  it("closes on Esc", () => {
    render(<Harness initialOpen={true} />);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("closes on overlay click but stays open when clicking inside", () => {
    render(<Harness initialOpen={true} />);
    const dialog = screen.getByRole("dialog");
    // Click inside the dialog, should NOT close.
    fireEvent.click(dialog);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    // Click on the overlay (the parent of the dialog), should close.
    const overlay = dialog.parentElement!;
    fireEvent.click(overlay);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("closes via the explicit Close button", () => {
    render(<Harness initialOpen={true} />);
    fireEvent.click(screen.getByRole("button", { name: /close/i }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("traps focus inside the panel: shift+tab from first wraps to last", () => {
    render(<Harness initialOpen={true} />);
    const closeBtn = screen.getByRole("button", { name: /close/i });
    closeBtn.focus();
    expect(document.activeElement).toBe(closeBtn);
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    // Last focusable is "last" button.
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "last" }),
    );
  });

  it("traps focus inside the panel: tab from last wraps to first", () => {
    render(<Harness initialOpen={true} />);
    const last = screen.getByRole("button", { name: "last" });
    last.focus();
    expect(document.activeElement).toBe(last);
    fireEvent.keyDown(document, { key: "Tab" });
    // First focusable is the close button.
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: /close/i }),
    );
  });

  it("locks body scroll while open and restores it on close", () => {
    const { unmount } = render(<Harness initialOpen={true} />);
    expect(document.body.style.overflow).toBe("hidden");
    fireEvent.click(screen.getByRole("button", { name: /close/i }));
    expect(document.body.style.overflow).toBe("");
    unmount();
  });
});
