import React from "react";
import { render, screen } from "@testing-library/react";

import SecondCta from "@/components/landing/SecondCta";

describe("<SecondCta />", () => {
  it("renders the spec heading verbatim", () => {
    render(<SecondCta />);
    expect(
      screen.getByRole("heading", { level: 2, name: /ready to see clearly\?/i }),
    ).toBeInTheDocument();
  });

  it("links the primary CTA to /register", () => {
    render(<SecondCta />);
    const link = screen.getByRole("link", { name: /get started free/i });
    expect(link).toHaveAttribute("href", "/register");
  });

  it("never contains an em-dash (locked policy)", () => {
    const { container } = render(<SecondCta />);
    expect(container.textContent).not.toMatch(/—/);
  });
});
