import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

import FounderCount from "@/components/landing/FounderCount";

const origTarget = process.env.NEXT_PUBLIC_BUILD_TARGET;
const origAppUrl = process.env.NEXT_PUBLIC_APP_URL;

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  process.env.NEXT_PUBLIC_BUILD_TARGET = origTarget;
  process.env.NEXT_PUBLIC_APP_URL = origAppUrl;
  vi.resetModules();
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
    // Same-origin (non-apex) build fetches the relative path.
    expect(fetch).toHaveBeenCalledWith("/api/v1/public/founder-count");
  });

  it("uses the singular noun at a count of 1", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => ({ count: 1 }) }),
    );
    render(<FounderCount />);
    expect(
      await screen.findByText(/1 founding member so far/),
    ).toBeInTheDocument();
  });

  it("fetches the absolute app-origin URL on the apex build", async () => {
    // The apex static host is a different origin from the app API, so the
    // counter MUST hit the absolute BRAND_APP_URL there. A regression that
    // always used the relative base would break the apex counter (the
    // primary deployment) yet pass the same-origin test above.
    delete process.env.NEXT_PUBLIC_APP_URL; // -> BRAND_APP_URL default
    process.env.NEXT_PUBLIC_BUILD_TARGET = "apex";
    vi.resetModules();
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, json: async () => ({ count: 5 }) });
    vi.stubGlobal("fetch", fetchMock);
    const { default: ApexFounderCount } = await import(
      "@/components/landing/FounderCount"
    );
    render(<ApexFounderCount />);
    await screen.findByText(/5 founding members so far/);
    expect(fetchMock).toHaveBeenCalledWith(
      "https://app.thebetterdecision.com/api/v1/public/founder-count",
    );
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
