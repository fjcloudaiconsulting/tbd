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
});
