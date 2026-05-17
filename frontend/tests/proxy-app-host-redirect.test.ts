/**
 * proxy.ts — app-host ``/ → /login`` redirect + security header stamping.
 *
 * The redirect previously lived in ``next.config.ts`` ``redirects()`` but
 * was moved here because Next.js routing short-circuits redirects()
 * BEFORE ``headers()`` applies, leaving the 307 without HSTS / nosniff /
 * Referrer-Policy / X-Frame-Options / Permissions-Policy. ZAP scan
 * 2026-05-16 confirmed the gap. Pinning here so a future move back to
 * ``redirects()`` would fail loudly.
 *
 * Companion: ``security-headers.test.ts`` (if added later) would pin
 * the constant values; this file pins behavior at the proxy boundary.
 */
import { describe, expect, it, vi } from "vitest";

import { NextRequest } from "next/server";

import { proxy, APP_HOST } from "@/proxy";

// Silence proxy()'s structured-log console.log noise during tests.
vi.spyOn(console, "log").mockImplementation(() => {});

function makeRequest(url: string, host: string): NextRequest {
  const req = new NextRequest(new Request(url, { method: "GET" }));
  // NextRequest preserves the URL's host, but we want to set the
  // Host header explicitly to match the production routing path.
  req.headers.set("host", host);
  return req;
}

describe("proxy: app-host root redirect", () => {
  it("redirects / on the app host to /login with status 307", () => {
    const res = proxy(
      makeRequest(`https://${APP_HOST}/`, APP_HOST),
    );
    expect(res.status).toBe(307);
    expect(res.headers.get("location")).toBe(`https://${APP_HOST}/login`);
  });

  it("does NOT redirect / on any other host (apex landing stays untouched)", () => {
    const res = proxy(
      makeRequest("https://thebetterdecision.com/", "thebetterdecision.com"),
    );
    // 200 from NextResponse.next() — not a redirect.
    expect(res.status).toBe(200);
    expect(res.headers.get("location")).toBeNull();
  });

  it("does NOT redirect non-root paths on the app host", () => {
    const res = proxy(
      makeRequest(`https://${APP_HOST}/dashboard`, APP_HOST),
    );
    expect(res.status).toBe(200);
    expect(res.headers.get("location")).toBeNull();
  });
});

describe("proxy: security headers on redirect response", () => {
  // Load-bearing invariant — the whole point of moving the redirect
  // out of next.config.ts. ZAP flagged HSTS-missing on this 307 path
  // before this change.
  function stampedHeaders() {
    const res = proxy(
      makeRequest(`https://${APP_HOST}/`, APP_HOST),
    );
    return res.headers;
  }

  it("stamps Strict-Transport-Security on the 307", () => {
    const headers = stampedHeaders();
    const hsts = headers.get("strict-transport-security");
    expect(hsts).toBeTruthy();
    expect(hsts).toContain("max-age=63072000");
    expect(hsts).toContain("includeSubDomains");
    expect(hsts).toContain("preload");
  });

  it("stamps X-Content-Type-Options: nosniff on the 307", () => {
    expect(stampedHeaders().get("x-content-type-options")).toBe("nosniff");
  });

  it("stamps X-Frame-Options: DENY on the 307", () => {
    expect(stampedHeaders().get("x-frame-options")).toBe("DENY");
  });

  it("stamps Referrer-Policy on the 307", () => {
    expect(stampedHeaders().get("referrer-policy")).toBe(
      "strict-origin-when-cross-origin",
    );
  });

  it("stamps Permissions-Policy on the 307", () => {
    const pp = stampedHeaders().get("permissions-policy");
    expect(pp).toContain("camera=()");
    expect(pp).toContain("microphone=()");
    expect(pp).toContain("geolocation=()");
    expect(pp).toContain("interest-cohort=()");
  });

  it("stamps the Cross-Origin pack on the 307", () => {
    const headers = stampedHeaders();
    expect(headers.get("cross-origin-opener-policy")).toBe("same-origin");
    expect(headers.get("cross-origin-resource-policy")).toBe("same-origin");
  });

  it("stamps Content-Security-Policy on the 307", () => {
    const csp = stampedHeaders().get("content-security-policy");
    expect(csp).toBeTruthy();
    expect(csp).toContain("default-src 'self'");
    expect(csp).toContain("frame-ancestors 'none'");
  });

  it("stamps a nonce-bearing script-src on the 307", () => {
    // The redirect path must carry the same per-request CSP shape as
    // any other response so a redirect-then-render flow stays
    // consistent. Pin the nonce regex; production CSP must NOT carry
    // ``'unsafe-inline'`` on script-src (ZAP Medium 2026-05-14).
    const csp = stampedHeaders().get("content-security-policy") ?? "";
    expect(csp).toMatch(/script-src[^;]*'nonce-[A-Za-z0-9+/=]+'/);
    expect(csp).toMatch(/script-src[^;]*'strict-dynamic'/);
  });
});

describe("proxy: security headers on regular responses", () => {
  // Defense-in-depth — next.config.ts headers() already covers these,
  // but stamping here too means a misconfigured headers() block can't
  // silently strip the pack.
  it("stamps the security pack on a non-redirect response", () => {
    const res = proxy(
      makeRequest(`https://${APP_HOST}/dashboard`, APP_HOST),
    );
    expect(res.headers.get("strict-transport-security")).toBeTruthy();
    expect(res.headers.get("x-content-type-options")).toBe("nosniff");
    expect(res.headers.get("x-frame-options")).toBe("DENY");
  });
});
