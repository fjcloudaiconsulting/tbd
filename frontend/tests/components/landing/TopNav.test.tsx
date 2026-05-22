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

  it("renders the spec-mandated Sign in + Get started auth links", () => {
    render(<TopNav />);
    expect(
      screen.getByRole("link", { name: /^sign in$/i }),
    ).toHaveAttribute("href", "/login");
    expect(
      screen.getByRole("link", { name: /^get started$/i }),
    ).toHaveAttribute("href", "/register");
    // Docs link from prior iteration must not appear here.
    expect(screen.queryByRole("link", { name: /docs/i })).toBeNull();
  });

  it("exposes the L5.1 in-page jump links to Pricing and FAQ", () => {
    render(<TopNav />);
    // Jump links target stable anchors on the long-form sections. They
    // are <a> tags (not Next <Link>) so the URL stays as a hash ref
    // even after client-side navigation.
    const pricing = screen.getByRole("link", { name: /^pricing$/i });
    expect(pricing).toHaveAttribute("href", "#pricing");
    const faq = screen.getByRole("link", { name: /^faq$/i });
    expect(faq).toHaveAttribute("href", "#faq");
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
