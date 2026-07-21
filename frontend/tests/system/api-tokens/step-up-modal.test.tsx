import { fireEvent, render, screen } from "@testing-library/react";

import StepUpModal from "@/components/system/api-tokens/StepUpModal";

describe("StepUpModal", () => {
  it("renders nothing when closed", () => {
    render(
      <StepUpModal
        open={false}
        passwordRequired
        mfaRequired={false}
        submitting={false}
        errorMessage={null}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("stepup-modal")).not.toBeInTheDocument();
  });

  it("collects a password when password step-up is required", () => {
    const onSubmit = vi.fn();
    render(
      <StepUpModal
        open
        passwordRequired
        mfaRequired={false}
        submitting={false}
        errorMessage={null}
        onSubmit={onSubmit}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.getByTestId("stepup-password")).toBeInTheDocument();
    expect(screen.queryByTestId("stepup-mfa")).not.toBeInTheDocument();

    fireEvent.change(screen.getByTestId("stepup-password"), {
      target: { value: "hunter2" },
    });
    fireEvent.click(screen.getByTestId("stepup-submit"));
    expect(onSubmit).toHaveBeenCalledWith({ current_password: "hunter2" });
  });

  it("collects a TOTP code when MFA is required, alongside the password", () => {
    const onSubmit = vi.fn();
    render(
      <StepUpModal
        open
        passwordRequired
        mfaRequired
        submitting={false}
        errorMessage={null}
        onSubmit={onSubmit}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.getByTestId("stepup-mfa")).toBeInTheDocument();
    fireEvent.change(screen.getByTestId("stepup-password"), {
      target: { value: "hunter2" },
    });
    fireEvent.change(screen.getByTestId("stepup-mfa"), {
      target: { value: "123456" },
    });
    fireEvent.click(screen.getByTestId("stepup-submit"));
    expect(onSubmit).toHaveBeenCalledWith({
      current_password: "hunter2",
      mfa_code: "123456",
    });
  });

  it("surfaces a step-up error message", () => {
    render(
      <StepUpModal
        open
        passwordRequired
        mfaRequired={false}
        submitting={false}
        errorMessage="Step-up verification required"
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.getByTestId("stepup-error")).toHaveTextContent(
      /step-up verification required/i,
    );
  });

  it("calls onCancel from the Cancel button", () => {
    const onCancel = vi.fn();
    render(
      <StepUpModal
        open
        passwordRequired
        mfaRequired={false}
        submitting={false}
        errorMessage={null}
        onSubmit={vi.fn()}
        onCancel={onCancel}
      />,
    );
    fireEvent.click(screen.getByTestId("stepup-cancel"));
    expect(onCancel).toHaveBeenCalled();
  });

  // SSO accounts (password_set === false) have no way to supply the
  // backend's required fresh `stepup_token` from this modal, so any submit
  // for them is unconditionally rejected with a 401. The modal must be
  // honest about that instead of collecting doomed proofs.
  it("shows an honest no-password message instead of a doomed submit when passwordRequired is false", () => {
    const onSubmit = vi.fn();
    render(
      <StepUpModal
        open
        passwordRequired={false}
        mfaRequired={false}
        submitting={false}
        errorMessage={null}
        onSubmit={onSubmit}
        onCancel={vi.fn()}
      />,
    );

    expect(screen.getByTestId("stepup-no-password-note")).toHaveTextContent(
      /requires a password on your account/i,
    );
    expect(
      screen.getByTestId("stepup-set-password-link"),
    ).toHaveAttribute("href", "/settings/security");

    // No mint-confirm action is offered at all.
    expect(screen.queryByTestId("stepup-submit")).not.toBeInTheDocument();
    expect(screen.queryByTestId("stepup-password")).not.toBeInTheDocument();
    expect(screen.queryByTestId("stepup-mfa")).not.toBeInTheDocument();

    // Nothing to submit, so onSubmit is never called.
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("shows the honest no-password message even when MFA is also enabled (still no proof the modal can supply)", () => {
    render(
      <StepUpModal
        open
        passwordRequired={false}
        mfaRequired
        submitting={false}
        errorMessage={null}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    expect(screen.getByTestId("stepup-no-password-note")).toBeInTheDocument();
    expect(screen.queryByTestId("stepup-submit")).not.toBeInTheDocument();
    expect(screen.queryByTestId("stepup-mfa")).not.toBeInTheDocument();
  });

  it("Cancel button reads Close (not Cancel) when there's nothing to submit", () => {
    render(
      <StepUpModal
        open
        passwordRequired={false}
        mfaRequired={false}
        submitting={false}
        errorMessage={null}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.getByTestId("stepup-cancel")).toHaveTextContent("Close");
  });
});
