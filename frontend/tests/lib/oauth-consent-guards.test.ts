/**
 * F-O13 (TBD-587): the MCP OAuth consent page at ``/oauth/authorize``.
 *
 * The login bounce goes through the EXISTING ``sanitizeReturnTo`` with no
 * OAuth-specific relaxation: a long consent query survives intact while the
 * protocol-relative and smuggled forms still fall back. The consent page must
 * never be frameable (clickjacking on consent): both header paths, the
 * ``next.config.ts`` ``headers()`` rule and the proxy's per-request pack,
 * cover it with ``frame-ancestors 'none'`` and ``X-Frame-Options: DENY``.
 */
import { describe, expect, it, vi } from "vitest";

import { NextRequest } from "next/server";

import { sanitizeReturnTo } from "@/lib/returnTo";
import nextConfig from "@/next.config";
import { proxy } from "@/proxy";

vi.spyOn(console, "log").mockImplementation(() => {});

const CONSENT = "/oauth/authorize";

const longConsent =
  `${CONSENT}?` +
  new URLSearchParams({
    client_id: "a".repeat(32),
    redirect_uri: "https://claude.ai/api/mcp/auth_callback",
    response_type: "code",
    code_challenge: "A".repeat(43),
    code_challenge_method: "S256",
    scope: "agent:read agent:write",
    resource: "https://app.thebetterdecision.com/mcp",
    state: "s".repeat(1000),
  }).toString();

// next.config ``source`` patterns used here are literals or a trailing
// ``/:path*`` (zero or more segments).
function sourceCovers(source: string, path: string): boolean {
  const literal = source.replace(/\/:path\*$/, "");
  const escaped = literal.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const tail = source.endsWith("/:path*") ? "(?:/.*)?" : "";
  return new RegExp(`^${escaped}${tail}$`).test(path);
}

describe("F-O13: OAuth consent login bounce", () => {
  it("keeps a long /oauth/authorize query intact", () => {
    expect(longConsent.length).toBeGreaterThan(1200);
    expect(sanitizeReturnTo(longConsent)).toBe(longConsent);
  });

  it.each([
    ["protocol-relative", "//evil.com"],
    ["backslash", "/\\evil.com"],
    ["tab-smuggled //", "/\t/evil.com"],
    ["protocol-relative with a consent path", `//evil.com${CONSENT}?client_id=x`],
    ["backslash with a consent path", `/\\evil.com${CONSENT}`],
    ["tab-smuggled with a consent path", `/\t/evil.com${CONSENT}`],
  ])("falls back for %s", (_label, raw) => {
    expect(sanitizeReturnTo(raw)).toBe("/dashboard");
  });
});

describe("F-O13: the consent page is never frameable", () => {
  it("a next.config headers() rule covers /oauth/authorize with frame-ancestors 'none'", async () => {
    expect(typeof nextConfig.headers).toBe("function");
    const rules = await nextConfig.headers!();
    const covering = rules.filter((r) => sourceCovers(r.source, CONSENT));
    expect(covering.length).toBeGreaterThan(0);
    for (const rule of covering) {
      const csp = rule.headers.find((h) => h.key === "Content-Security-Policy")?.value;
      expect(csp).toContain("frame-ancestors 'none'");
      expect(rule.headers.find((h) => h.key === "X-Frame-Options")?.value).toBe("DENY");
    }
  });

  it("the proxy stamps frame-ancestors 'none' and DENY on /oauth/authorize", () => {
    const res = proxy(new NextRequest(new Request(`https://example.com${longConsent}`)));
    expect(res.headers.get("content-security-policy")).toContain("frame-ancestors 'none'");
    expect(res.headers.get("x-frame-options")).toBe("DENY");
  });
});
