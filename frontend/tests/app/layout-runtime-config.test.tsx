import { renderToStaticMarkup } from "react-dom/server";

vi.mock("@/lib/nonce", () => ({ readNonce: async () => "n1" }));
vi.mock("@/components/auth/AuthProvider", () => ({ AuthProvider: ({ children }: { children: React.ReactNode }) => children }));
vi.mock("@/components/OrgCurrencyBoundary", () => ({ default: ({ children }: { children: React.ReactNode }) => children }));
vi.mock("@/components/ThemeProvider", () => ({ ThemeProvider: ({ children }: { children: React.ReactNode }) => children }));
vi.mock("@/components/tour/TourProvider", () => ({ TourProvider: ({ children }: { children: React.ReactNode }) => children }));

import RootLayout from "@/app/layout";

// Fence: without this script the client falls back to empty env and the SSO
// button / Turnstile widget disappear (and hydration mismatches).
it("root layout emits the nonce'd runtime config script", async () => {
  vi.stubEnv("TBD_CAPTCHA_SITE_KEY", "site-key-xyz");
  const html = renderToStaticMarkup(await RootLayout({ children: null }));
  expect(html).toContain('window.__TBD_CONFIG__={');
  expect(html).toContain("site-key-xyz");
  expect(html).toMatch(/<script[^>]*nonce="n1"[^>]*>window\.__TBD_CONFIG__/);
  vi.unstubAllEnvs();
});
