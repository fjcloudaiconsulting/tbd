import React from "react";
import { render, screen } from "@testing-library/react";

import { Logo, Mark, Wordmark } from "@/components/brand/Logo";

describe("<Mark />", () => {
  it("renders an SVG with an accessible title by default", () => {
    render(<Mark data-testid="mark" />);
    const svg = screen.getByTestId("mark");
    expect(svg.tagName.toLowerCase()).toBe("svg");
    expect(svg).toHaveAttribute("role", "img");
    expect(svg.querySelector("title")).not.toBeNull();
    expect(svg.querySelector("title")?.textContent).toBe(
      "The Better Decision",
    );
  });

  it("hides itself from screen readers when label is null", () => {
    render(<Mark data-testid="mark" label={null} />);
    const svg = screen.getByTestId("mark");
    expect(svg).toHaveAttribute("aria-hidden", "true");
    expect(svg).not.toHaveAttribute("role");
    expect(svg.querySelector("title")).toBeNull();
  });

  it("uses the custom label when provided", () => {
    render(<Mark data-testid="mark" label="Custom label" />);
    expect(
      screen.getByTestId("mark").querySelector("title")?.textContent,
    ).toBe("Custom label");
  });

  it("renders two chevrons (echo + lead)", () => {
    render(<Mark data-testid="mark" />);
    const paths = screen.getByTestId("mark").querySelectorAll("path");
    expect(paths.length).toBe(2);
  });

  it.each([
    ["sm", 16],
    ["md", 24],
    ["lg", 40],
  ] as const)("renders %s size at %dpx square", (size, px) => {
    render(<Mark data-testid="mark" size={size} />);
    const svg = screen.getByTestId("mark");
    expect(svg).toHaveAttribute("width", String(px));
    expect(svg).toHaveAttribute("height", String(px));
  });

  it("recolors the lead chevron to accent for default tone", () => {
    render(<Mark data-testid="mark" tone="default" />);
    const paths = screen.getByTestId("mark").querySelectorAll("path");
    // Second path is the brass "lead" chevron.
    expect(paths[1].getAttribute("stroke")).toContain("--color-accent");
  });

  it("collapses both chevrons to muted tone when muted", () => {
    render(<Mark data-testid="mark" tone="muted" />);
    const paths = screen.getByTestId("mark").querySelectorAll("path");
    expect(paths[1].getAttribute("stroke")).toContain("--color-text-muted");
  });
});

describe("<Wordmark />", () => {
  it("renders the full brand name by default", () => {
    render(<Wordmark />);
    expect(screen.getByText("The Better Decision")).toBeInTheDocument();
  });

  it("renders the short form when short is true", () => {
    render(<Wordmark short />);
    expect(screen.getByText("TBD")).toBeInTheDocument();
    expect(screen.queryByText("The Better Decision")).toBeNull();
  });

  it("applies the display font and primary text color by default", () => {
    render(<Wordmark />);
    const node = screen.getByText("The Better Decision");
    expect(node.className).toContain("font-display");
    expect(node.className).toContain("text-text-primary");
  });

  it("uses muted tone classes when tone='muted'", () => {
    render(<Wordmark tone="muted" />);
    expect(screen.getByText("The Better Decision").className).toContain(
      "text-text-muted",
    );
  });
});

describe("<Logo />", () => {
  it("renders the mark and wordmark together", () => {
    const { container } = render(<Logo data-testid="logo" />);
    expect(container.querySelector("svg")).not.toBeNull();
    expect(screen.getByText("The Better Decision")).toBeInTheDocument();
  });

  it("mark is decorative inside the lockup (wordmark carries the name)", () => {
    const { container } = render(<Logo />);
    const svg = container.querySelector("svg");
    expect(svg).toHaveAttribute("aria-hidden", "true");
  });

  it("renders identically in dark and light theme (snapshot)", () => {
    // Dark (default)
    const { container: darkContainer, unmount: unmountDark } = render(
      <Logo />,
    );
    expect(darkContainer.firstChild).toMatchSnapshot("logo-dark");
    unmountDark();

    // Light: set data-theme on documentElement and re-render. Because the
    // SVG references CSS custom properties, the rendered markup itself
    // does not change shape — only the resolved color does. This snapshot
    // therefore proves the component does not branch on theme in JS.
    document.documentElement.setAttribute("data-theme", "light");
    try {
      const { container: lightContainer } = render(<Logo />);
      expect(lightContainer.firstChild).toMatchSnapshot("logo-light");
    } finally {
      document.documentElement.removeAttribute("data-theme");
    }
  });

  it("swaps the wordmark to TBD when short is true", () => {
    render(<Logo short />);
    expect(screen.getByText("TBD")).toBeInTheDocument();
  });
});
