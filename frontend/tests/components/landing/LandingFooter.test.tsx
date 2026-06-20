import React from "react";
import { render, screen } from "@testing-library/react";

import LandingFooter from "@/components/landing/LandingFooter";

describe("<LandingFooter />", () => {
  it("renders the muted brand lockup with a copyright line", () => {
    const { container } = render(<LandingFooter />);
    // Logo wordmark text appears via the brand component.
    expect(screen.getByText("The Better Decision")).toBeInTheDocument();
    // © character + current year span.
    expect(container.textContent).toMatch(/©/);
    expect(container.textContent).toMatch(/\d{4}/);
  });

  it("links Privacy, Terms, Help to their routes", () => {
    render(<LandingFooter />);
    expect(
      screen.getByRole("link", { name: /^privacy$/i }),
    ).toHaveAttribute("href", "/privacy");
    expect(
      screen.getByRole("link", { name: /^terms$/i }),
    ).toHaveAttribute("href", "/terms");
    // The "Help" label routes to /docs (the existing in-app user manual
    // from PR #159) — there is no /help route, and a public 404 is not
    // acceptable for launch polish.
    expect(
      screen.getByRole("link", { name: /^help$/i }),
    ).toHaveAttribute("href", "/docs");
  });

  it("exposes the contact mailto", () => {
    render(<LandingFooter />);
    const mail = screen.getByRole("link", {
      name: /hello@thebetterdecision\.com/i,
    });
    expect(mail).toHaveAttribute(
      "href",
      "mailto:hello@thebetterdecision.com",
    );
  });

  it("exposes a Cookie preferences control to re-open the consent banner", () => {
    render(<LandingFooter />);
    expect(
      screen.getByRole("button", { name: /cookie preferences/i }),
    ).toBeInTheDocument();
  });

  it("uses a labelled footer nav", () => {
    render(<LandingFooter />);
    expect(
      screen.getByRole("navigation", { name: /footer/i }),
    ).toBeInTheDocument();
  });

  it("never contains an em-dash", () => {
    const { container } = render(<LandingFooter />);
    expect(container.textContent).not.toMatch(/—/);
  });

  it("snapshot stays stable in dark + light", () => {
    const { container, unmount } = render(<LandingFooter />);
    expect(container.firstChild).toMatchSnapshot("footer-dark");
    unmount();
    document.documentElement.setAttribute("data-theme", "light");
    try {
      const { container: light } = render(<LandingFooter />);
      expect(light.firstChild).toMatchSnapshot("footer-light");
    } finally {
      document.documentElement.removeAttribute("data-theme");
    }
  });
});
