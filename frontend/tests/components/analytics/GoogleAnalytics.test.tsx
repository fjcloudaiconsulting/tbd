import React from "react";
import { render } from "@testing-library/react";

// GA4 env vars are module-level consts read at import time, so we must
// set process.env and call vi.resetModules() BEFORE dynamically importing
// the module under test. Pattern mirrors build-apex.test.ts's loadLinks().

const origTarget = process.env.NEXT_PUBLIC_BUILD_TARGET;
const origMeasurementId = process.env.NEXT_PUBLIC_GA_MEASUREMENT_ID;

afterEach(() => {
  // Restore original env vars and flush module registry.
  if (origTarget === undefined) {
    delete process.env.NEXT_PUBLIC_BUILD_TARGET;
  } else {
    process.env.NEXT_PUBLIC_BUILD_TARGET = origTarget;
  }
  if (origMeasurementId === undefined) {
    delete process.env.NEXT_PUBLIC_GA_MEASUREMENT_ID;
  } else {
    process.env.NEXT_PUBLIC_GA_MEASUREMENT_ID = origMeasurementId;
  }
  vi.resetModules();
});

describe("GoogleAnalytics — non-apex build", () => {
  it("renders nothing when NEXT_PUBLIC_BUILD_TARGET is not 'apex'", async () => {
    delete process.env.NEXT_PUBLIC_BUILD_TARGET;
    delete process.env.NEXT_PUBLIC_GA_MEASUREMENT_ID;
    vi.resetModules();

    const { GoogleAnalytics } = await import(
      "@/components/analytics/GoogleAnalytics"
    );

    const { container } = render(<GoogleAnalytics />);

    // No gtag scripts in the rendered container.
    const scripts = container.querySelectorAll("script");
    const gtagScripts = Array.from(scripts).filter(
      (s) => s.getAttribute("src")?.includes("/vd9r/") ?? false,
    );
    expect(gtagScripts.length).toBe(0);
    // Component returns null so the container should be empty.
    expect(container.firstChild).toBeNull();
  });
});

describe("GoogleAnalytics — apex build", () => {
  it("renders GA4 scripts for the apex build", async () => {
    process.env.NEXT_PUBLIC_BUILD_TARGET = "apex";
    process.env.NEXT_PUBLIC_GA_MEASUREMENT_ID = "G-GRXDVTVBLV";
    vi.resetModules();

    const { GoogleAnalytics } = await import(
      "@/components/analytics/GoogleAnalytics"
    );

    render(<GoogleAnalytics />);

    // React 19 hoists <script async src="..."> to document.head as a
    // resource. Check both the container and the document for the scripts.
    const allScripts = Array.from(
      document.querySelectorAll("script"),
    );

    // The async loader script src must be the first-party tag-gateway path
    // (served via CloudFront), NOT googletagmanager.com.
    const loaderScript = allScripts.find(
      (s) => (s.getAttribute("src") ?? "") === "/vd9r/",
    );
    expect(
      loaderScript,
      `expected a <script src="/vd9r/"> (first-party gateway loader) in document.scripts but found: ${allScripts.map((s) => s.outerHTML.substring(0, 120)).join(" | ")}`,
    ).toBeDefined();
    // And it must NOT load from the third-party googletagmanager host.
    const thirdParty = allScripts.find((s) =>
      (s.getAttribute("src") ?? "").includes("googletagmanager.com"),
    );
    expect(thirdParty).toBeUndefined();

    // The inline config script must call gtag('config', ...) with the ID.
    const inlineScript = allScripts.find((s) =>
      (s.textContent ?? s.innerHTML ?? "").includes(
        "gtag('config', 'G-GRXDVTVBLV')",
      ),
    );
    expect(
      inlineScript,
      `expected an inline gtag config script in document but found: ${allScripts.map((s) => s.outerHTML.substring(0, 120)).join(" | ")}`,
    ).toBeDefined();
  });
});
