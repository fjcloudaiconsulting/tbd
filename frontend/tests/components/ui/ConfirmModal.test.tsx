import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";

import ConfirmModal from "@/components/ui/ConfirmModal";

// Contract test for the shared confirm dialog: focus trap / restoration /
// scroll-lock a11y behavior is exercised indirectly through its many callers
// (e.g. tests/app/categories-drag-drop.test.tsx for DragMoveConfirmModal).
// This file locks down the `submitting` prop added for the PAT revoke flow
// (frontend/app/system/api-tokens/page.tsx) and proves the default (unset)
// path — used by every pre-existing caller — is unaffected.
describe("ConfirmModal", () => {
  it("renders confirm/cancel enabled by default with no submitting prop", () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <ConfirmModal
        open
        title="Delete item"
        message="Are you sure?"
        confirmLabel="Delete"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />,
    );

    const confirmBtn = screen.getByRole("button", { name: "Delete" });
    const cancelBtn = screen.getByRole("button", { name: "Cancel" });
    expect(confirmBtn).not.toBeDisabled();
    expect(cancelBtn).not.toBeDisabled();

    fireEvent.click(confirmBtn);
    expect(onConfirm).toHaveBeenCalledTimes(1);

    fireEvent.click(cancelBtn);
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("disables both buttons and swaps the confirm label when submitting", () => {
    render(
      <ConfirmModal
        open
        title="Revoke token"
        message="Revoke it?"
        confirmLabel="Revoke token"
        variant="danger"
        submitting
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: "Revoke token" })).not.toBeInTheDocument();
    const confirmBtn = screen.getByRole("button", { name: "Working…" });
    const cancelBtn = screen.getByRole("button", { name: "Cancel" });
    expect(confirmBtn).toBeDisabled();
    expect(cancelBtn).toBeDisabled();
  });

  it("renders nothing when closed", () => {
    render(
      <ConfirmModal
        open={false}
        title="Delete item"
        message="Are you sure?"
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

// F11 (TBD-273). Edit-next puts an autoFocus amount input inside the dialog.
// Kills: the confirm auto-focus stealing focus from that input (the user would
// type into nothing), and a `children` change that moves focus off confirm for
// the existing callers.
describe("ConfirmModal focus with children (TBD-273)", () => {
  it("F11: without children, confirm takes focus", () => {
    render(<ConfirmModal open title="T" message="M" confirmLabel="Go" onConfirm={vi.fn()} onCancel={vi.fn()} />);
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Go" }));
  });

  it("F11: an autoFocus input child keeps focus, and focus returns to the trigger on close", () => {
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button onClick={() => setOpen(true)}>Open</button>
          <ConfirmModal open={open} title="T" message="M" confirmLabel="Go" onConfirm={vi.fn()} onCancel={() => setOpen(false)}>
            <input aria-label="Amount" autoFocus />
          </ConfirmModal>
        </>
      );
    }
    render(<Harness />);
    const trigger = screen.getByRole("button", { name: "Open" });
    trigger.focus();
    fireEvent.click(trigger);
    expect(document.activeElement).toBe(screen.getByLabelText("Amount"));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(document.activeElement).toBe(trigger);
  });

  it("confirmDisabled disables only the confirm button", () => {
    render(<ConfirmModal open title="T" message="M" confirmLabel="Go" confirmDisabled onConfirm={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Go" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).not.toBeDisabled();
  });
});
