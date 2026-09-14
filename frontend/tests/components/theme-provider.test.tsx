import React from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";

import { ThemeProvider, useTheme } from "@/components/ThemeProvider";

function ThemeProbe() {
  const { theme, toggle } = useTheme();
  return (
    <div>
      <span data-testid="theme">{theme}</span>
      <button onClick={toggle}>toggle</button>
    </div>
  );
}

function renderProvider() {
  return render(
    <ThemeProvider>
      <ThemeProbe />
    </ThemeProvider>,
  );
}

const attr = () => document.documentElement.getAttribute("data-theme");

describe("ThemeProvider (light is the default, TBD-429)", () => {
  afterEach(() => {
    // ThemeProvider mutates the documentElement; reset between tests so a
    // light run does not bleed into the next.
    act(() => {
      document.documentElement.removeAttribute("data-theme");
    });
  });

  it("defaults to light with no stored value", () => {
    renderProvider();

    expect(screen.getByTestId("theme").textContent).toBe("light");
    expect(attr()).toBe("light");
    expect(window.localStorage.getItem("tbd-theme")).toBeNull();
  });

  it("a stored 'dark' wins, and clears the pre-paint light attribute", () => {
    window.localStorage.setItem("tbd-theme", "dark");
    document.documentElement.setAttribute("data-theme", "light");

    renderProvider();

    expect(screen.getByTestId("theme").textContent).toBe("dark");
    expect(attr()).toBeNull();
  });

  it("a stored 'light' reads as light", () => {
    window.localStorage.setItem("tbd-theme", "light");

    renderProvider();

    expect(screen.getByTestId("theme").textContent).toBe("light");
    expect(attr()).toBe("light");
  });

  it("a garbage stored value falls back to light", () => {
    window.localStorage.setItem("tbd-theme", "DARK");

    renderProvider();

    expect(screen.getByTestId("theme").textContent).toBe("light");
    expect(attr()).toBe("light");
  });

  it("toggling persists to tbd-theme in both directions", () => {
    renderProvider();

    fireEvent.click(screen.getByRole("button", { name: "toggle" }));
    expect(screen.getByTestId("theme").textContent).toBe("dark");
    expect(window.localStorage.getItem("tbd-theme")).toBe("dark");
    expect(attr()).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "toggle" }));
    expect(screen.getByTestId("theme").textContent).toBe("light");
    expect(window.localStorage.getItem("tbd-theme")).toBe("light");
    expect(attr()).toBe("light");
  });

  it("survives storage that throws (Safari SecurityError): light, and the toggle still works", () => {
    const blocked = () => {
      throw new DOMException("The operation is insecure.", "SecurityError");
    };
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(blocked);
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(blocked);
    const errors: unknown[] = [];
    const onError = (e: ErrorEvent) => {
      errors.push(e.error);
      e.preventDefault();
    };
    window.addEventListener("error", onError);
    try {
      renderProvider();
      expect(getItem).toHaveBeenCalled();
      expect(screen.getByTestId("theme").textContent).toBe("light");
      expect(attr()).toBe("light");

      fireEvent.click(screen.getByRole("button", { name: "toggle" }));
      expect(setItem).toHaveBeenCalled();
      expect(screen.getByTestId("theme").textContent).toBe("dark");
      expect(attr()).toBeNull();

      fireEvent.click(screen.getByRole("button", { name: "toggle" }));
      expect(attr()).toBe("light");
      expect(errors).toEqual([]);
    } finally {
      window.removeEventListener("error", onError);
      getItem.mockRestore();
      setItem.mockRestore();
    }
  });

  it("does not read the legacy pfv2-theme key", () => {
    // A leftover legacy entry must be ignored; visitor falls back to default.
    window.localStorage.setItem("pfv2-theme", "dark");

    renderProvider();

    expect(screen.getByTestId("theme").textContent).toBe("light");
    expect(attr()).toBe("light");
  });
});
