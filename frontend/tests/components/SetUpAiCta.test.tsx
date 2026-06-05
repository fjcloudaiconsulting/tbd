import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { SetUpAiCta } from "@/components/ai/SetUpAiCta";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
  usePathname: () => "/",
}));

describe("SetUpAiCta", () => {
  it("admin sees a link to settings", () => {
    render(<SetUpAiCta role="owner" />);
    const link = screen.getByRole("link", { name: /set up ai/i });
    expect(link).toHaveAttribute("href", "/settings/ai-providers");
  });
  it("member sees an ask-admin message, no link", () => {
    render(<SetUpAiCta role="member" />);
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText(/ask your.*admin/i)).toBeInTheDocument();
  });
});
