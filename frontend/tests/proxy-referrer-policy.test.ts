import { NextRequest } from "next/server";

import { proxy } from "@/proxy";

/**
 * FE-03. Pages that receive a single-use token in the query string keep it in
 * the URL until the client strips it after hydration. Every subresource the
 * browser fetches before then sends that URL as Referer (same-origin requests
 * get the full URL under strict-origin-when-cross-origin), where access logs
 * record it. These routes answer with Referrer-Policy: no-referrer.
 */
describe("proxy Referrer-Policy on one-time token routes", () => {
  const logSpy = vi.spyOn(console, "log").mockImplementation(() => {});

  afterAll(() => {
    logSpy.mockRestore();
  });

  it.each([
    "/reset-password?token=t",
    "/verify-email?token=t",
    "/accept-invite?token=t",
    "/mfa-verify?mfa_token=t",
    "/settings/ai-providers/openrouter/callback?code=c&state=s",
  ])("sends no-referrer on %s", (path) => {
    const response = proxy(new NextRequest(`https://example.com${path}`));
    expect(response.headers.get("Referrer-Policy")).toBe("no-referrer");
  });

  it.each(["/login", "/dashboard", "/reset-password-help", "/forgot-password"])(
    "keeps the default policy on %s",
    (path) => {
      const response = proxy(new NextRequest(`https://example.com${path}`));
      expect(response.headers.get("Referrer-Policy")).toBe(
        "strict-origin-when-cross-origin",
      );
    },
  );
});
