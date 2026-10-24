import { renderHook } from "@testing-library/react";

import { useOneTimeQueryParam } from "@/lib/hooks/use-one-time-query-param";

// Like Next.js, read the live URL: after the strip the param is gone from
// useSearchParams too, so the hook must keep the value it captured.
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(window.location.search),
}));

afterEach(() => {
  window.history.replaceState(null, "", "/");
  vi.restoreAllMocks();
});

describe("useOneTimeQueryParam (FE-03)", () => {
  it("returns the token and strips only that param from the address bar", () => {
    window.history.replaceState(null, "", "/reset-password?token=abc123&next=%2Fx#frag");

    const { result, rerender } = renderHook(() => useOneTimeQueryParam("token"));

    expect(result.current).toBe("abc123");
    expect(window.location.pathname).toBe("/reset-password");
    expect(window.location.search).toBe("?next=%2Fx");
    expect(window.location.hash).toBe("#frag");
    expect(window.location.href).not.toContain("abc123");

    rerender();
    expect(result.current).toBe("abc123");
  });

  it("drops the whole query string when the token was the only param", () => {
    window.history.replaceState(null, "", "/accept-invite?token=inv");

    renderHook(() => useOneTimeQueryParam("token"));

    expect(window.location.search).toBe("");
    expect(window.location.pathname).toBe("/accept-invite");
  });

  it("returns null and leaves history alone when the param is absent", () => {
    window.history.replaceState(null, "", "/reset-password?other=1");
    const spy = vi.spyOn(window.history, "replaceState");

    const { result } = renderHook(() => useOneTimeQueryParam("token"));

    expect(result.current).toBeNull();
    expect(spy).not.toHaveBeenCalled();
    expect(window.location.search).toBe("?other=1");
  });
});
