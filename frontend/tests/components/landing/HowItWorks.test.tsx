import { render, screen } from "@testing-library/react";

import HowItWorks from "@/components/landing/HowItWorks";

describe("<HowItWorks />", () => {
  it("renders an ordered list of three numbered steps", () => {
    render(<HowItWorks />);
    const list = screen.getByRole("list");
    expect(list.tagName).toBe("OL");
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(3);
    expect(items[0]).toHaveTextContent(/step 1/i);
    expect(items[1]).toHaveTextContent(/step 2/i);
    expect(items[2]).toHaveTextContent(/step 3/i);
  });

  it("renders each step title and a distinctive body snippet", () => {
    render(<HowItWorks />);
    const items = screen.getAllByRole("listitem");

    expect(items[0]).toHaveTextContent(/connect your accounts/i);
    expect(items[0]).toHaveTextContent(/import a csv from your bank/i);

    expect(items[1]).toHaveTextContent(/categorize as you go/i);
    expect(items[1]).toHaveTextContent(/auto-categorization learns from your edits/i);

    expect(items[2]).toHaveTextContent(/see what comes next/i);
    expect(items[2]).toHaveTextContent(/recurring bills, forecasts/i);
  });

  it("uses a region landmark with an accessible name", () => {
    render(<HowItWorks />);
    expect(
      screen.getByRole("region", { name: /how the better decision works/i }),
    ).toBeInTheDocument();
  });

  it("contains no em-dashes (locked voice policy)", () => {
    render(<HowItWorks />);
    // toHaveTextContent walks the full subtree; the section root carries
    // every word the section ships. If a future copy edit introduces an
    // em-dash, this assertion flags it before the page-level guard does.
    const section = screen.getByRole("region", {
      name: /how the better decision works/i,
    });
    expect(section.textContent ?? "").not.toMatch(/—/);
  });
});
