import React from "react";
import { render, screen } from "@testing-library/react";

import Hero from "@/components/landing/Hero";

describe("<Hero />", () => {
  it("uses the locked tagline verbatim in the single page <h1>", () => {
    render(<Hero />);
    const heading = screen.getByRole("heading", { level: 1 });
    // The headline uses a <br /> for visual line break; textContent
    // concatenates without injecting whitespace. Check both halves
    // are present in order.
    const text = heading.textContent?.replace(/\s+/g, "").trim() ?? "";
    expect(text).toContain("There’snobestdecision.");
    expect(text).toContain("Onlybetterones.");
  });

  it("renders the kicker as the brand name", () => {
    render(<Hero />);
    expect(screen.getByText(/^The Better Decision$/)).toBeInTheDocument();
  });

  it("uses spec sub-copy without em-dashes", () => {
    render(<Hero />);
    const sub = screen.getByText(
      /forecasts your cash flow and plans your budget in one calm app/i,
    );
    expect(sub).toBeInTheDocument();
    // Hard guarantee: no em-dash anywhere in the hero (locked policy).
    expect(sub.textContent).not.toMatch(/—/);
  });

  it("links the primary CTA to /register and secondary to /login", () => {
    render(<Hero />);
    expect(
      screen.getByRole("link", { name: /get started free/i }),
    ).toHaveAttribute("href", "/register");
    expect(
      screen.getByRole("link", { name: /^sign in$/i }),
    ).toHaveAttribute("href", "/login");
  });

  it("scales the headline via clamp() inline (responsive editorial lift)", () => {
    render(<Hero />);
    const heading = screen.getByRole("heading", { level: 1 });
    // Tailwind arbitrary value `text-[clamp(...)]` lands in the className.
    expect(heading.className).toMatch(/clamp\(2\.5rem,5vw,4rem\)/);
  });
});
