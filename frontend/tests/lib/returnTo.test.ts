import { sanitizeReturnTo } from "@/lib/returnTo";

describe("sanitizeReturnTo", () => {
  it("honors a plain same-origin path", () => {
    expect(sanitizeReturnTo("/transactions")).toBe("/transactions");
  });

  it("honors a nested path with a query string", () => {
    expect(sanitizeReturnTo("/reports/123?x=1")).toBe("/reports/123?x=1");
  });

  it("honors a URL-encoded same-origin path", () => {
    // AppShell encodes the returnTo before putting it in the query string.
    expect(sanitizeReturnTo(encodeURIComponent("/reports/123?x=1"))).toBe(
      "/reports/123?x=1",
    );
  });

  it.each([
    ["protocol-relative //evil.com", "//evil.com"],
    ["backslash-tricked /\\evil.com", "/\\evil.com"],
    ["absolute https URL", "https://evil.com"],
    ["absolute http URL", "http://evil.com"],
    ["javascript: scheme", "javascript:alert(1)"],
    ["the login route itself", "/login"],
    ["the setup route itself", "/setup"],
    ["an empty string", ""],
  ])("falls back to /dashboard for %s", (_label, raw) => {
    expect(sanitizeReturnTo(raw)).toBe("/dashboard");
  });

  it("falls back to /dashboard for null / undefined", () => {
    expect(sanitizeReturnTo(null)).toBe("/dashboard");
    expect(sanitizeReturnTo(undefined)).toBe("/dashboard");
  });

  it("falls back to /dashboard for an encoded protocol-relative URL", () => {
    // Guard against an attacker who URL-encodes the leading slashes to
    // sneak a protocol-relative redirect past a naive prefix check.
    expect(sanitizeReturnTo(encodeURIComponent("//evil.com"))).toBe(
      "/dashboard",
    );
  });

  it("falls back to /dashboard for a malformed percent-encoding", () => {
    // A lone `%` throws in decodeURIComponent; must not leak the error.
    expect(sanitizeReturnTo("%E0%A4%A")).toBe("/dashboard");
  });

  it("does not treat /login-adjacent paths as the login route", () => {
    // Exact-match guard only: /loginhelp is a legitimate app path.
    expect(sanitizeReturnTo("/loginhelp")).toBe("/loginhelp");
  });
});
