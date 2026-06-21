import { fireEvent, render, screen, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import Hero from "@/components/landing/Hero";
import SecondCta from "@/components/landing/SecondCta";
import { trackRegisterClick } from "@/lib/analytics";

// Guards against a valid-but-wrong cta_location copy-pasted across the four
// signup CTA call sites — the SignupCtaLocation type cannot catch e.g.
// SecondCta passing location="hero". The conversion (event name) is unaffected,
// but the GA4 cta_location breakdown would be corrupted.

vi.mock("@/lib/analytics", async (importActual) => ({
  ...(await importActual<typeof import("@/lib/analytics")>()),
  trackRegisterClick: vi.fn(),
}));

// Hero pulls ThemeProvider transitively; stub it so the render is light.
vi.mock("@/components/ThemeProvider", () => ({
  useTheme: () => ({ theme: "dark", toggle: vi.fn() }),
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function clickWithoutNavigating(name: RegExp) {
  const link = screen.getByRole("link", { name });
  link.addEventListener("click", (e) => e.preventDefault());
  fireEvent.click(link);
}

describe("signup CTA location wiring", () => {
  it("Hero fires register_click with location 'hero'", () => {
    render(<Hero />);
    clickWithoutNavigating(/get started free/i);
    expect(trackRegisterClick).toHaveBeenCalledWith("hero");
  });

  it("SecondCta fires register_click with location 'second_cta'", () => {
    render(<SecondCta />);
    clickWithoutNavigating(/get started free/i);
    expect(trackRegisterClick).toHaveBeenCalledWith("second_cta");
  });
});
