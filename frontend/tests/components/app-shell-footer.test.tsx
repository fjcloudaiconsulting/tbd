import React from "react";
import { render, screen } from "@testing-library/react";

import AppShellFooter from "@/components/AppShellFooter";

describe("<AppShellFooter />", () => {
  it("renders the muted brand lockup with a copyright line", () => {
    const { container } = render(<AppShellFooter />);
    // Logo wordmark text appears via the brand component.
    expect(screen.getByText("The Better Decision")).toBeInTheDocument();
    // © character + current year span.
    expect(container.textContent).toMatch(/©/);
    expect(container.textContent).toMatch(/\d{4}/);
  });

  it("links Privacy, Terms, Help to their routes", () => {
    render(<AppShellFooter />);
    expect(
      screen.getByRole("link", { name: /^privacy$/i }),
    ).toHaveAttribute("href", "/privacy");
    expect(
      screen.getByRole("link", { name: /^terms$/i }),
    ).toHaveAttribute("href", "/terms");
    // /docs is the existing in-app user manual (PR #159). The Help
    // label reuses LandingFooter's convention so a public 404 stays
    // off-table at launch.
    expect(
      screen.getByRole("link", { name: /^help$/i }),
    ).toHaveAttribute("href", "/docs");
  });

  it("exposes the contact mailto", () => {
    render(<AppShellFooter />);
    const mail = screen.getByRole("link", {
      name: /hello@thebetterdecision\.com/i,
    });
    expect(mail).toHaveAttribute(
      "href",
      "mailto:hello@thebetterdecision.com",
    );
  });

  it("uses a labelled footer nav distinct from the landing footer", () => {
    render(<AppShellFooter />);
    // "App footer" lets assistive tech distinguish this from the
    // landing footer (which uses aria-label="Footer") when both are
    // navigable in a single document during a future shared-layout
    // refactor.
    expect(
      screen.getByRole("navigation", { name: /app footer/i }),
    ).toBeInTheDocument();
  });

  it("never contains an em-dash or a PFV/pfv2 remnant", () => {
    const { container } = render(<AppShellFooter />);
    expect(container.textContent).not.toMatch(/—/);
    // Brand sweep: the authed shell footer must never surface internal
    // codenames. localStorage keys and event names live elsewhere.
    expect(container.textContent).not.toMatch(/\bPFV\b/);
    expect(container.textContent).not.toMatch(/\bpfv2?\b/i);
  });

  it("uses middle dots as link separators, not em-dashes", () => {
    const { container } = render(<AppShellFooter />);
    expect(container.textContent).toMatch(/·/);
  });

  it("snapshot stays stable in dark + light", () => {
    const { container, unmount } = render(<AppShellFooter />);
    expect(container.firstChild).toMatchSnapshot("app-footer-dark");
    unmount();
    document.documentElement.setAttribute("data-theme", "light");
    try {
      const { container: light } = render(<AppShellFooter />);
      expect(light.firstChild).toMatchSnapshot("app-footer-light");
    } finally {
      document.documentElement.removeAttribute("data-theme");
    }
  });
});
