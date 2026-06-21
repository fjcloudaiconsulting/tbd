import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import SignupLink from "@/components/landing/SignupLink";
import { trackRegisterClick } from "@/lib/analytics";

vi.mock("@/lib/analytics", async (importActual) => ({
  ...(await importActual<typeof import("@/lib/analytics")>()),
  trackRegisterClick: vi.fn(),
}));

afterEach(() => vi.clearAllMocks());

describe("SignupLink", () => {
  it("renders an anchor to the signup href", () => {
    render(
      <SignupLink location="hero" className="cta">
        Get started free
      </SignupLink>,
    );
    const link = screen.getByRole("link", { name: "Get started free" });
    expect(link).toHaveAttribute("href", "/register"); // non-apex test build
    expect(link).toHaveClass("cta");
  });

  it("fires trackRegisterClick with its location on click", () => {
    render(<SignupLink location="topnav">Get started</SignupLink>);
    const link = screen.getByRole("link", { name: "Get started" });
    // Cancel jsdom's (unimplemented) navigation; the component's onClick still fires.
    link.addEventListener("click", (e) => e.preventDefault());
    fireEvent.click(link);
    expect(trackRegisterClick).toHaveBeenCalledWith("topnav");
  });
});
