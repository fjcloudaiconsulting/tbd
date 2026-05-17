import { NextRequest, NextResponse } from "next/server";

import { securityHeadersTuplesWithNonce } from "./lib/security-headers";

/**
 * Next.js middleware — logs every request in structured JSON format
 * matching the backend/nginx log style for unified observability, and
 * stamps the security header pack onto every response.
 *
 * Sensitive query parameters (tokens, codes) are stripped from logs.
 *
 * Why headers stamping lives here in addition to ``next.config.ts``
 * ``headers()``: Next.js's routing pipeline emits redirect responses
 * (e.g. the app-host ``/ → /login`` 307) BEFORE ``headers()`` applies,
 * so those redirects ship without HSTS / nosniff / Referrer-Policy.
 * ZAP scan 2026-05-16 confirmed the gap. Stamping here covers the
 * redirect path; ``headers()`` covers the rest, and double-stamping
 * the same values on non-redirect responses is idempotent.
 *
 * CSP nonces: a fresh base64 nonce is generated per request, threaded
 * to the renderer via the ``x-nonce`` request header (which Next.js
 * forwards to Server Components via the ``headers()`` API), and baked
 * into the Content-Security-Policy header on the response. The
 * production CSP no longer permits ``'unsafe-inline'`` on script-src
 * or style-src (ZAP scan 2026-05-14 Mediums). See
 * ``lib/security-headers.ts`` ``buildCspDirectives``.
 */

export const APP_HOST = "app.thebetterdecision.com";

const NONCE_HEADER = "x-nonce";

/**
 * Generate a per-request CSP nonce. The output is a 24-character
 * base64 string (16 raw bytes of entropy), which is well above the
 * "unpredictable" bar and small enough not to bloat the header.
 *
 * The MDN guidance is "unique per response" + "cryptographically
 * random"; ``crypto.getRandomValues`` provides the latter and the
 * per-request invocation ensures the former.
 */
function generateNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  // btoa is available in the Edge runtime that Next.js uses for
  // middleware; Buffer would also work but is heavier.
  return btoa(binary);
}

function applySecurityHeaders(
  response: NextResponse,
  nonce: string,
): NextResponse {
  for (const [name, value] of securityHeadersTuplesWithNonce(nonce)) {
    response.headers.set(name, value);
  }
  return response;
}

const SENSITIVE_PARAMS = new Set([
  "token", "code", "access_token", "refresh_token", "mfa_token", "key", "secret", "password",
]);

function sanitizeQuery(search: string): string | undefined {
  if (!search) return undefined;
  const params = new URLSearchParams(search);
  for (const key of params.keys()) {
    if (SENSITIVE_PARAMS.has(key.toLowerCase())) {
      params.set(key, "[REDACTED]");
    }
  }
  const result = params.toString();
  return result || undefined;
}

function clientIp(request: NextRequest): string {
  const xff = request.headers.get("x-forwarded-for");
  if (xff) return xff.split(",")[0].trim();
  return request.headers.get("x-real-ip") || "unknown";
}

// Bounded inbound id; mirrors the backend RequestContextMiddleware
// policy. Past this we generate a fresh UUID rather than trusting
// caller-supplied bytes — keeps log size sane and avoids passing
// pathological values upstream.
const REQUEST_ID_MAX_LEN = 64;
const REQUEST_ID_RE = /^[\w.\-]+$/;

function coerceRequestId(raw: string | null): string {
  if (raw && raw.length > 0 && raw.length <= REQUEST_ID_MAX_LEN && REQUEST_ID_RE.test(raw)) {
    return raw;
  }
  return crypto.randomUUID().replace(/-/g, "");
}

export function proxy(request: NextRequest) {
  const inbound = request.headers.get("x-request-id");
  const requestId = coerceRequestId(inbound);
  const nonce = generateNonce();

  // App-host root → /login. Moved here from ``next.config.ts``
  // ``redirects()`` because that config-level redirect short-circuits
  // before ``headers()`` applies (ZAP scan 2026-05-16). Stamping the
  // security pack on the 307 closes the cold-cache TLS-downgrade
  // window for first-time visitors. ``app/page.tsx`` is shared with
  // the apex static export (built via ``next.config.apex.ts``); host-
  // scoping ensures we don't accidentally redirect on the apex
  // landing page.
  if (
    request.nextUrl.pathname === "/" &&
    request.headers.get("host") === APP_HOST
  ) {
    const url = request.nextUrl.clone();
    url.pathname = "/login";
    const redirect = NextResponse.redirect(url, 307);
    redirect.headers.set("x-request-id", requestId);
    applySecurityHeaders(redirect, nonce);
    return redirect;
  }

  // Forward the id and the per-request CSP nonce to the renderer.
  // The backend's RequestContextMiddleware uses an inbound
  // X-Request-Id verbatim when reasonable, so this gives us end-to-end
  // correlation across frontend → nginx → backend. The nonce is read
  // from ``headers()`` in ``app/layout.tsx`` and attached to inline
  // ``<script>`` tags so they pass the strict CSP.
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-request-id", requestId);
  requestHeaders.set(NONCE_HEADER, nonce);
  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("x-request-id", requestId);
  applySecurityHeaders(response, nonce);

  const entry = {
    timestamp: new Date().toISOString(),
    level: "info",
    logger: "frontend.access",
    request_id: requestId,
    method: request.method,
    path: request.nextUrl.pathname,
    query: sanitizeQuery(request.nextUrl.search),
    remote_addr: clientIp(request),
    user_agent: request.headers.get("user-agent") || undefined,
    referer: request.headers.get("referer") || undefined,
  };

  // Remove undefined values for cleaner JSON
  const clean = Object.fromEntries(
    Object.entries(entry).filter(([, v]) => v !== undefined)
  );

  console.log(JSON.stringify(clean));

  return response;
}

export const config = {
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|icon.svg|.*\\.(?:png|jpg|jpeg|gif|webp|svg|ico)$).*)",
  ],
};
