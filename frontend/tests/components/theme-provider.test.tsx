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

describe("ThemeProvider localStorage key", () => {
  afterEach(() => {
    // ThemeProvider mutates the documentElement; reset between tests so a
    // light run does not bleed into the next.
    act(() => {
      document.documentElement.removeAttribute("data-theme");
    });
  });

  it("reads the stored theme from tbd-theme on mount", async () => {
    window.localStorage.setItem("tbd-theme", "light");

    render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>,
    );

    expect(await screen.findByText("light")).toBeInTheDocument();
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  });

  it("writes the toggled theme to tbd-theme", () => {
    render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>,
    );

    // Default theme is dark, no stored value.
    expect(window.localStorage.getItem("tbd-theme")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "toggle" }));

    expect(window.localStorage.getItem("tbd-theme")).toBe("light");
  });

  it("does not read the legacy pfv2-theme key", () => {
    // A leftover legacy entry must be ignored; visitor falls back to default.
    window.localStorage.setItem("pfv2-theme", "light");

    render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>,
    );

    // Default is dark, and the legacy key must not change that.
    expect(screen.getByTestId("theme").textContent).toBe("dark");
    expect(document.documentElement.getAttribute("data-theme")).not.toBe(
      "light",
    );
  });
});
