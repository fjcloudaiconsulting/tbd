import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
  delete (window as unknown as { gtag?: unknown }).gtag;
});

async function loadAnalytics(buildTarget?: string) {
  vi.resetModules();
  if (buildTarget) vi.stubEnv("NEXT_PUBLIC_BUILD_TARGET", buildTarget);
  return import("@/lib/analytics");
}

describe("trackRegisterClick", () => {
  it("fires a register_click GA4 event on the apex build when gtag exists", async () => {
    const gtag = vi.fn();
    (window as unknown as { gtag?: unknown }).gtag = gtag;
    const { trackRegisterClick } = await loadAnalytics("apex");
    trackRegisterClick("hero");
    expect(gtag).toHaveBeenCalledWith("event", "register_click", {
      cta_location: "hero",
      transport_type: "beacon",
    });
  });

  it("no-ops when not the apex build", async () => {
    const gtag = vi.fn();
    (window as unknown as { gtag?: unknown }).gtag = gtag;
    const { trackRegisterClick } = await loadAnalytics(); // unset target
    trackRegisterClick("hero");
    expect(gtag).not.toHaveBeenCalled();
  });

  it("does not throw when gtag is absent on the apex build", async () => {
    const { trackRegisterClick } = await loadAnalytics("apex");
    expect(() => trackRegisterClick("topnav")).not.toThrow();
  });
});
