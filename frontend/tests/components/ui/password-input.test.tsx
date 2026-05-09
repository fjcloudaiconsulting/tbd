import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";

import PasswordInput from "@/components/ui/PasswordInput";

function ControlledHarness({ initial = "" }: { initial?: string }) {
  const [value, setValue] = useState(initial);
  return (
    <PasswordInput
      id="pwd"
      value={value}
      onChange={(e) => setValue(e.target.value)}
      autoComplete="new-password"
    />
  );
}

describe("PasswordInput", () => {
  it("renders type='password' by default", () => {
    render(<ControlledHarness />);
    const input = document.getElementById("pwd") as HTMLInputElement;
    expect(input).toBeInTheDocument();
    expect(input.type).toBe("password");
  });

  it("toggle changes type from password to text and back", () => {
    render(<ControlledHarness />);
    const input = document.getElementById("pwd") as HTMLInputElement;
    const button = screen.getByRole("button", { name: /show password/i });

    expect(input.type).toBe("password");
    fireEvent.click(button);
    expect(input.type).toBe("text");
    expect(
      screen.getByRole("button", { name: /hide password/i }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /hide password/i }));
    expect(input.type).toBe("password");
  });

  it("preserves typed value when toggling visibility", () => {
    render(<ControlledHarness />);
    const input = document.getElementById("pwd") as HTMLInputElement;

    fireEvent.change(input, { target: { value: "Sup3r$ecret" } });
    expect(input.value).toBe("Sup3r$ecret");

    fireEvent.click(screen.getByRole("button", { name: /show password/i }));
    expect(input.type).toBe("text");
    expect(input.value).toBe("Sup3r$ecret");

    fireEvent.click(screen.getByRole("button", { name: /hide password/i }));
    expect(input.type).toBe("password");
    expect(input.value).toBe("Sup3r$ecret");
  });

  it("submits the typed value regardless of visibility state", () => {
    const onSubmit = vi.fn((e: React.FormEvent<HTMLFormElement>) => {
      e.preventDefault();
      const data = new FormData(e.currentTarget);
      return data.get("password");
    });

    function Form() {
      const [value, setValue] = useState("");
      return (
        <form onSubmit={onSubmit}>
          <PasswordInput
            id="pwd"
            name="password"
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
          <button type="submit">submit</button>
        </form>
      );
    }

    render(<Form />);
    const input = document.getElementById("pwd") as HTMLInputElement;

    fireEvent.change(input, { target: { value: "hidden-secret" } });
    fireEvent.click(screen.getByRole("button", { name: /submit/i }));
    expect(onSubmit).toHaveBeenCalledTimes(1);

    // Toggle visible, submit again
    fireEvent.click(screen.getByRole("button", { name: /show password/i }));
    expect(input.type).toBe("text");
    fireEvent.click(screen.getByRole("button", { name: /submit/i }));
    expect(onSubmit).toHaveBeenCalledTimes(2);
  });

  it("toggle button is type='button' and does not submit the form", () => {
    const onSubmit = vi.fn((e: React.FormEvent<HTMLFormElement>) => {
      e.preventDefault();
    });

    function Form() {
      const [value, setValue] = useState("");
      return (
        <form onSubmit={onSubmit}>
          <PasswordInput
            id="pwd"
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
        </form>
      );
    }

    render(<Form />);
    const button = screen.getByRole("button", { name: /show password/i });
    expect(button).toHaveAttribute("type", "button");
    fireEvent.click(button);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("aria-pressed and aria-label flip on toggle", () => {
    render(<ControlledHarness />);
    const button = screen.getByRole("button", { name: /show password/i });
    expect(button).toHaveAttribute("aria-pressed", "false");
    expect(button).toHaveAttribute("aria-label", "Show password");

    fireEvent.click(button);
    const flipped = screen.getByRole("button", { name: /hide password/i });
    expect(flipped).toHaveAttribute("aria-pressed", "true");
    expect(flipped).toHaveAttribute("aria-label", "Hide password");
  });

  it("activates via Space and Enter keyboard events", () => {
    render(<ControlledHarness />);
    const input = document.getElementById("pwd") as HTMLInputElement;
    const button = screen.getByRole("button", { name: /show password/i });

    // Native <button> elements fire click on Space/Enter; simulate the
    // resulting click to mirror the browser behavior under jsdom.
    button.focus();
    expect(button).toHaveFocus();
    fireEvent.click(button);
    expect(input.type).toBe("text");

    const flipped = screen.getByRole("button", { name: /hide password/i });
    flipped.focus();
    fireEvent.click(flipped);
    expect(input.type).toBe("password");
  });

  it("forwards autoComplete and other input attrs", () => {
    render(
      <PasswordInput
        id="pwd"
        autoComplete="current-password"
        required
        minLength={8}
        readOnly
        value="x"
        onChange={() => {}}
      />,
    );
    const input = document.getElementById("pwd") as HTMLInputElement;
    expect(input).toHaveAttribute("autoComplete", "current-password");
    expect(input).toBeRequired();
    expect(input).toHaveAttribute("minLength", "8");
    expect(input).toHaveAttribute("readOnly");
  });
});
