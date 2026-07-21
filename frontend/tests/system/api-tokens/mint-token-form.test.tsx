import { fireEvent, render, screen } from "@testing-library/react";

import MintTokenForm from "@/components/system/api-tokens/MintTokenForm";

describe("MintTokenForm", () => {
  it("defaults to read scope and 30-day expiry", () => {
    render(<MintTokenForm onSubmit={vi.fn()} />);
    expect(screen.getByTestId("mint-scope-read")).toBeChecked();
    expect(screen.getByTestId("mint-scope-write")).not.toBeChecked();
    expect((screen.getByTestId("mint-expiry") as HTMLSelectElement).value).toBe("30");
  });

  it("offers exactly the 7 / 30 / 60 / 90 expiry presets", () => {
    render(<MintTokenForm onSubmit={vi.fn()} />);
    const select = screen.getByTestId("mint-expiry") as HTMLSelectElement;
    const values = Array.from(select.options).map((o) => o.value);
    expect(values).toEqual(["7", "30", "60", "90"]);
  });

  it("does not submit when the name is blank", () => {
    const onSubmit = vi.fn();
    render(<MintTokenForm onSubmit={onSubmit} />);
    fireEvent.submit(screen.getByTestId("mint-form"));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByTestId("mint-name-error")).toBeInTheDocument();
  });

  it("submits the chosen name, scope and expiry", () => {
    const onSubmit = vi.fn();
    render(<MintTokenForm onSubmit={onSubmit} />);
    fireEvent.change(screen.getByTestId("mint-name"), {
      target: { value: "local scripting" },
    });
    fireEvent.click(screen.getByTestId("mint-scope-write"));
    fireEvent.change(screen.getByTestId("mint-expiry"), { target: { value: "90" } });
    fireEvent.submit(screen.getByTestId("mint-form"));
    expect(onSubmit).toHaveBeenCalledWith({
      name: "local scripting",
      scope: "write",
      expiresInDays: 90,
    });
  });

  it("disables the submit button while submitting", () => {
    render(<MintTokenForm onSubmit={vi.fn()} submitting />);
    expect(screen.getByTestId("mint-submit")).toBeDisabled();
  });
});
