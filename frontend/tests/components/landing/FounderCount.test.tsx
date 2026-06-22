import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

import FounderCount from "@/components/landing/FounderCount";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("FounderCount", () => {
  it("renders the live count after a successful fetch", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => ({ count: 142 }) }),
    );
    render(<FounderCount />);
    expect(
      await screen.findByText(/142 founding members so far/),
    ).toBeInTheDocument();
  });

  it("renders nothing on fetch error (no fake fallback number)", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("boom")));
    const { container } = render(<FounderCount />);
    await waitFor(() => {});
    expect(container.textContent ?? "").not.toMatch(/founding members/);
  });

  it("renders nothing when the count is zero", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => ({ count: 0 }) }),
    );
    const { container } = render(<FounderCount />);
    await waitFor(() => {});
    expect(container.textContent ?? "").not.toMatch(/founding members/);
  });
});
