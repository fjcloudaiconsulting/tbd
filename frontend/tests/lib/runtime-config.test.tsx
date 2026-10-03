import { render } from "@testing-library/react";

import { RuntimeConfigScript } from "@/components/RuntimeConfigScript";
import { runtimeConfig } from "@/lib/runtime-config";

afterEach(() => {
  vi.unstubAllEnvs();
  delete window.__TBD_CONFIG__;
});

describe("runtime config", () => {
  it("reads env at call time, not import time (fence: one image, many envs)", () => {
    vi.stubEnv("TBD_GOOGLE_SSO_ENABLED", "false");
    expect(runtimeConfig().googleSsoEnabled).toBe(false);
    vi.stubEnv("TBD_GOOGLE_SSO_ENABLED", "true");
    expect(runtimeConfig().googleSsoEnabled).toBe(true);
  });

  it("client prefers the server-injected window config over env", () => {
    vi.stubEnv("TBD_CAPTCHA_SITE_KEY", "from-env");
    window.__TBD_CONFIG__ = { apiUrl: "", googleSsoEnabled: false, captchaSiteKey: "from-window", appVersion: "v1" };
    expect(runtimeConfig().captchaSiteKey).toBe("from-window");
  });

  it("defaults: empty api url, sso off, version dev", () => {
    expect(runtimeConfig()).toEqual({ apiUrl: "", googleSsoEnabled: false, captchaSiteKey: "", appVersion: "dev" });
  });

  it("script serialises current env with a nonce and cannot be closed by a value", () => {
    vi.stubEnv("TBD_APP_VERSION", "</script><b>x");
    const { container } = render(<RuntimeConfigScript nonce="n1" />);
    const el = container.querySelector("script")!;
    expect(el.getAttribute("nonce")).toBe("n1");
    expect(el.innerHTML).not.toContain("</script>");
    const cfg = new Function(`var window={};${el.innerHTML};return window.__TBD_CONFIG__;`)();
    expect(cfg.appVersion).toBe("</script><b>x");
  });
});
