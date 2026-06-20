import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

// isApexBuild is a module-level const read from NEXT_PUBLIC_BUILD_TARGET at
// import time, so set the env + resetModules BEFORE dynamically importing the
// component (same pattern as GoogleAnalytics.test.tsx).
const origTarget = process.env.NEXT_PUBLIC_BUILD_TARGET;

afterEach(() => {
  if (origTarget === undefined) {
    delete process.env.NEXT_PUBLIC_BUILD_TARGET;
  } else {
    process.env.NEXT_PUBLIC_BUILD_TARGET = origTarget;
  }
  vi.resetModules();
});

describe("<CookiePreferencesButton />", () => {
  it("renders nothing on a non-apex build (no banner to re-open)", async () => {
    delete process.env.NEXT_PUBLIC_BUILD_TARGET;
    vi.resetModules();
    const { default: CookiePreferencesButton } = await import(
      "@/components/landing/CookiePreferencesButton"
    );
    const { container } = render(<CookiePreferencesButton />);
    expect(container.firstChild).toBeNull();
  });

  it("renders on apex and dispatches the open-consent event on click", async () => {
    process.env.NEXT_PUBLIC_BUILD_TARGET = "apex";
    vi.resetModules();
    const [{ default: CookiePreferencesButton }, { CONSENT_OPEN_EVENT }] =
      await Promise.all([
        import("@/components/landing/CookiePreferencesButton"),
        import("@/lib/consent"),
      ]);

    const handler = vi.fn();
    window.addEventListener(CONSENT_OPEN_EVENT, handler);
    try {
      render(<CookiePreferencesButton />);
      const btn = screen.getByRole("button", { name: /cookie preferences/i });
      fireEvent.click(btn);
      expect(handler).toHaveBeenCalledTimes(1);
    } finally {
      window.removeEventListener(CONSENT_OPEN_EVENT, handler);
    }
  });
});
