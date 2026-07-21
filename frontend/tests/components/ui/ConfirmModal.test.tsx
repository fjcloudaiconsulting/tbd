import { fireEvent, render, screen } from "@testing-library/react";

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
