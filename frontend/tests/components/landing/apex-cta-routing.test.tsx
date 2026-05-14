import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";

// apex-cta-routing.test.tsx — verifies that under NEXT_PUBLIC_BUILD_TARGET=apex
// the landing CTAs render as absolute URLs pointing at BRAND_APP_URL,
// because the apex host and the app host are different origins and
// relative paths would 404 there.
//
// `lib/links.ts` reads NEXT_PUBLIC_BUILD_TARGET at module evaluation
// time. To get a fresh evaluation per test we call vi.resetModules()
// between env permutations and re-import the landing component.

vi.mock("@/components/ThemeProvider", () => ({
  useTheme: () => ({ theme: "dark", toggle: vi.fn() }),
}));

const origTarget = process.env.NEXT_PUBLIC_BUILD_TARGET;
const origAppUrl = process.env.NEXT_PUBLIC_APP_URL;

async function renderLanding(
  importPath: string,
  env: { target?: string; appUrl?: string },
) {
  delete process.env.NEXT_PUBLIC_BUILD_TARGET;
  delete process.env.NEXT_PUBLIC_APP_URL;
  if (env.target !== undefined) process.env.NEXT_PUBLIC_BUILD_TARGET = env.target;
  if (env.appUrl !== undefined) process.env.NEXT_PUBLIC_APP_URL = env.appUrl;
  vi.resetModules();
  // Re-import the ThemeProvider mock binding after resetModules so the
  // mock factory above continues to apply.
  vi.doMock("@/components/ThemeProvider", () => ({
    useTheme: () => ({ theme: "dark", toggle: vi.fn() }),
  }));
  const mod = await import(importPath);
  const Component = mod.default as React.ComponentType;
  return render(<Component />);
}

afterEach(() => {
  cleanup();
  if (origTarget === undefined) {
    delete process.env.NEXT_PUBLIC_BUILD_TARGET;
  } else {
    process.env.NEXT_PUBLIC_BUILD_TARGET = origTarget;
  }
  if (origAppUrl === undefined) {
    delete process.env.NEXT_PUBLIC_APP_URL;
  } else {
    process.env.NEXT_PUBLIC_APP_URL = origAppUrl;
  }
  vi.resetModules();
  vi.doUnmock("@/components/ThemeProvider");
});

describe("landing CTAs under apex build target", () => {
  it("TopNav uses absolute app URLs in the apex build", async () => {
    await renderLanding("@/components/landing/TopNav", { target: "apex" });
    expect(screen.getByRole("link", { name: /^sign in$/i })).toHaveAttribute(
      "href",
      "https://app.thebetterdecision.com/login",
    );
    expect(screen.getByRole("link", { name: /^get started$/i })).toHaveAttribute(
      "href",
      "https://app.thebetterdecision.com/register",
    );
  });

  it("Hero uses absolute app URLs in the apex build", async () => {
    await renderLanding("@/components/landing/Hero", { target: "apex" });
    expect(
      screen.getByRole("link", { name: /get started free/i }),
    ).toHaveAttribute("href", "https://app.thebetterdecision.com/register");
    expect(screen.getByRole("link", { name: /^sign in$/i })).toHaveAttribute(
      "href",
      "https://app.thebetterdecision.com/login",
    );
  });

  it("SecondCta uses absolute app URLs in the apex build", async () => {
    await renderLanding("@/components/landing/SecondCta", { target: "apex" });
    expect(
      screen.getByRole("link", { name: /get started free/i }),
    ).toHaveAttribute("href", "https://app.thebetterdecision.com/register");
  });

  it("respects NEXT_PUBLIC_APP_URL override in the apex build", async () => {
    await renderLanding("@/components/landing/TopNav", {
      target: "apex",
      appUrl: "https://staging.example.com",
    });
    expect(screen.getByRole("link", { name: /^sign in$/i })).toHaveAttribute(
      "href",
      "https://staging.example.com/login",
    );
  });

  it("keeps relative paths in the standard app build (no target)", async () => {
    await renderLanding("@/components/landing/TopNav", {});
    expect(screen.getByRole("link", { name: /^sign in$/i })).toHaveAttribute(
      "href",
      "/login",
    );
    expect(screen.getByRole("link", { name: /^get started$/i })).toHaveAttribute(
      "href",
      "/register",
    );
  });
});
