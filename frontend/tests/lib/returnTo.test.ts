import { sanitizeReturnTo } from "@/lib/returnTo";

describe("sanitizeReturnTo", () => {
  it("honors a plain same-origin path", () => {
    expect(sanitizeReturnTo("/transactions")).toBe("/transactions");
  });

  it("honors a nested path with a query string", () => {
    expect(sanitizeReturnTo("/reports/123?x=1")).toBe("/reports/123?x=1");
  });

  it("preserves the fragment alongside the query string", () => {
    expect(sanitizeReturnTo("/reports/123?x=1#section")).toBe(
      "/reports/123?x=1#section",
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

  // ---------------------------------------------------------------------
  // Control-character open-redirect bypass (CRITICAL). Browsers and the
  // WHATWG URL parser strip U+0009 (tab), U+000A (LF), U+000D (CR) while
  // parsing, so a value that LOOKS like a same-origin "/⇥/evil.com"
  // collapses to the protocol-relative "//evil.com" at navigation time and
  // redirects cross-origin. sanitizeReturnTo receives the value already
  // decoded once by searchParams.get, so we cover BOTH the raw
  // percent-encoded form (never decoded a second time → must not start with
  // "/") AND the already-decoded control-char form (must be caught before
  // it reaches router.replace).
  // ---------------------------------------------------------------------
  it.each([
    // Percent-encoded forms as they would appear pre-decode. Since the
    // sanitizer no longer decodes a second time, these stay literal, do NOT
    // start with "/", and fall back.
    ["encoded tab //evil %2F%09%2Fevil.com", "%2F%09%2Fevil.com"],
    ["encoded LF //evil %2F%0A%2Fevil.com", "%2F%0A%2Fevil.com"],
    ["encoded CR //evil %2F%0D%2Fevil.com", "%2F%0D%2Fevil.com"],
    ["encoded tab+backslash %2F%09%5Cevil.com", "%2F%09%5Cevil.com"],
    // Already-decoded control-char forms (what searchParams.get yields).
    ["raw tab /\\t/evil.com", "/\t/evil.com"],
    ["raw LF /\\n/evil.com", "/\n/evil.com"],
    ["raw CR /\\r/evil.com", "/\r/evil.com"],
    ["raw tab+backslash /\\t\\\\evil.com", "/\t\\evil.com"],
  ])("blocks control-char smuggled redirect: %s", (_label, raw) => {
    expect(sanitizeReturnTo(raw)).toBe("/dashboard");
  });

  it("falls back to /dashboard for an encoded protocol-relative URL", () => {
    // An attacker who URL-encodes the leading slashes now hands us a value
    // that does not start with "/" (the caller already decoded once), so it
    // never reaches the origin check.
    expect(sanitizeReturnTo("%2F%2Fevil.com")).toBe("/dashboard");
  });

  it("does not treat /login-adjacent paths as the login route", () => {
    // Exact-match guard only: /loginhelp is a legitimate app path.
    expect(sanitizeReturnTo("/loginhelp")).toBe("/loginhelp");
  });

  it("does not treat /setup-adjacent paths as the setup route", () => {
    expect(sanitizeReturnTo("/setuphelp")).toBe("/setuphelp");
  });

  // ---------------------------------------------------------------------
  // Dot-segment protocol-relative open-redirect bypass (CRITICAL). The
  // origin check runs against the PARSED URL, but the sanitizer returns a
  // RECONSTRUCTED string. Dot-segment normalization can make u.pathname
  // begin with "//" while the sentinel origin stays intact, so the returned
  // value is protocol-relative and re-resolves cross-origin when Next's
  // router parses it against the real app origin.
  // ---------------------------------------------------------------------
  it.each([
    ["dot-dot slash //evil.com", "/..//evil.com"],
    ["dot slash //evil.com", "/.//evil.com"],
    ["nested dot-dot //evil.com", "/x/..//evil.com"],
    ["encoded dot-dot %2e%2e//evil.com", "/%2e%2e//evil.com"],
    ["deep dot-dot //evil.com", "/foo/../..//evil.com"],
    ["backslash dot-dot /x/..\\evil.com", "/x/..\\evil.com"],
  ])("blocks dot-segment protocol-relative redirect: %s", (_label, raw) => {
    expect(sanitizeReturnTo(raw)).toBe("/dashboard");
  });

  it.each([
    // "//" that lives in the query or fragment is not protocol-relative:
    // the path stays same-origin, so these must be PRESERVED intact.
    ["query with //", "/path?a=//evil.com", "/path?a=//evil.com"],
    ["fragment with //", "/path#//evil.com", "/path#//evil.com"],
    // A legit dot-segment that normalizes to a plain same-origin path.
    ["legit dot-segment /a/b/../c → /a/c", "/a/b/../c", "/a/c"],
  ])("allows a legitimate value: %s", (_label, raw, expected) => {
    expect(sanitizeReturnTo(raw)).toBe(expected);
  });

  it("does not double-decode a legitimate literal percent path", () => {
    // Regression: the old sanitizer called decodeURIComponent a SECOND time
    // on the already-decoded value, so a literal "%" (e.g. "50%-growth")
    // threw and silently fell back to /dashboard. It must now survive as a
    // real same-origin path.
    const out = sanitizeReturnTo("/reports/50%-growth");
    expect(out).not.toBe("/dashboard");
    expect(out).toBe("/reports/50%-growth");
  });
});
