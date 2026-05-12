import React from "react";
import { render, screen } from "@testing-library/react";

import TopNav from "@/components/landing/TopNav";

// ThemeProvider context backs ThemeToggle. Mock it so the nav renders
// in isolation without needing the surrounding provider tree.
vi.mock("@/components/ThemeProvider", () => ({
  useTheme: () => ({ theme: "dark", toggle: vi.fn() }),
}));

describe("<TopNav />", () => {
  it("renders the brand lockup as the home link", () => {
    render(<TopNav />);
    const homeLink = screen.getByRole("link", {
      name: /the better decision, home/i,
    });
    expect(homeLink).toHaveAttribute("href", "/");
    // The Logo wordmark should appear inside the home link.
    expect(homeLink.querySelector("svg")).not.toBeNull();
    expect(homeLink).toHaveTextContent("The Better Decision");
  });

  it("renders only the spec-mandated Sign in + Get started links", () => {
    render(<TopNav />);
    expect(
      screen.getByRole("link", { name: /^sign in$/i }),
    ).toHaveAttribute("href", "/login");
    expect(
      screen.getByRole("link", { name: /^get started$/i }),
    ).toHaveAttribute("href", "/register");
    // Docs link from prior iteration must not appear — spec §3.1 lists
    // only Sign in + Get started + theme toggle.
    expect(screen.queryByRole("link", { name: /docs/i })).toBeNull();
  });

  it("exposes the theme toggle button", () => {
    render(<TopNav />);
    expect(
      screen.getByRole("button", { name: /switch to (light|dark) mode/i }),
    ).toBeInTheDocument();
  });

  it("uses a <nav> landmark labelled Primary", () => {
    render(<TopNav />);
    expect(
      screen.getByRole("navigation", { name: /primary/i }),
    ).toBeInTheDocument();
  });

  it("snapshot stays stable across theme runs", () => {
    const { container, unmount } = render(<TopNav />);
    expect(container.firstChild).toMatchSnapshot("topnav-dark");
    unmount();
    document.documentElement.setAttribute("data-theme", "light");
    try {
      const { container: light } = render(<TopNav />);
      expect(light.firstChild).toMatchSnapshot("topnav-light");
    } finally {
      document.documentElement.removeAttribute("data-theme");
    }
  });
});
