/**
 * Shared security-header constants for the Next.js frontend.
 *
 * Consumed by both ``next.config.ts`` (which applies the pack to
 * regular page responses via ``headers()``) and ``proxy.ts`` (which
 * applies them to redirect responses that Next.js's routing pipeline
 * short-circuits before ``headers()`` would fire).
 *
 * Why duplication exists at all: ZAP scan 2026-05-16 confirmed that
 * the ``/ → /login`` 307 redirect emitted by ``next.config.ts``
 * ``redirects()`` was NOT receiving the headers from ``headers()``.
 * Moving the redirect into ``proxy.ts`` and stamping the pack there
 * closes the cold-cache TLS-downgrade window for first-time visitors.
 *
 * CSP is split into two flavors:
 *   * ``buildCspDirectives(nonce)`` produces the per-request CSP with
 *     a nonce baked into script-src and style-src. Used by ``proxy.ts``
 *     which generates a fresh nonce per request.
 *   * ``cspDirectives`` is the no-nonce fallback used by
 *     ``next.config.ts`` ``headers()`` for any code path that never
 *     transits the proxy (none today, but a defensive default).
 *     Production drops ``'unsafe-inline'`` from script-src entirely.
 *     ZAP scan 2026-05-14 flagged both Mediums for ``'unsafe-inline'``;
 *     ``proxy.ts`` overrides with the per-request CSP so the no-nonce
 *     constant only fires if the proxy is bypassed somehow.
 */

const isDev = process.env.NODE_ENV !== "production";

// If the frontend is deployed with a cross-origin API URL (e.g.,
// separate DO App Platform components), allow that origin in
// connect-src so api.ts fetch() calls aren't blocked. Empty string →
// same-origin via nginx, nothing extra to allow.
function apiOrigin(): string {
  const raw = process.env.NEXT_PUBLIC_API_URL;
  if (!raw) return "";
  try {
    return new URL(raw).origin;
  } catch {
    return "";
  }
}

/**
 * Build the per-request CSP value.
 *
 * Production:
 *   script-src 'self' 'nonce-<X>' 'strict-dynamic'
 *   style-src  'self' 'nonce-<X>' https://fonts.googleapis.com
 *
 *   ``'strict-dynamic'`` lets the initial bundle (which carries the
 *   nonce) load further scripts without each one needing its own
 *   nonce. Combined with omitting ``'unsafe-inline'``, this closes
 *   the two ZAP Mediums while keeping Next.js hydration working.
 *
 *   Style-src keeps the Google Fonts origin allowlisted and adds the
 *   nonce so Next.js's runtime-emitted ``<style>`` tags execute.
 *
 * Development:
 *   Keeps ``'unsafe-inline'`` (and adds ``'unsafe-eval'`` on script-src)
 *   for HMR / Fast Refresh and React's eval-based dev error stacks.
 *   The nonce is added too, so ``app/layout.tsx`` can attach it
 *   unconditionally without forking dev vs prod render paths.
 */
export function buildCspDirectives(nonce: string): string {
  const scriptSrc = isDev
    ? `script-src 'self' 'nonce-${nonce}' 'strict-dynamic' 'unsafe-inline' 'unsafe-eval'`
    : `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'`;
  // ``style-src`` covers ``<style>`` elements (where Next.js attaches
  // the nonce). ``style-src-attr`` is a SEPARATE directive that covers
  // inline ``style="..."`` attributes; browsers do NOT fall back from
  // ``style-src-attr`` to ``style-src`` for inline attribute values.
  // React renders many inline ``style`` props across the app (dashboard
  // charts, tour, modals, transient layout sizing); refactoring them
  // all out of inline is a much larger change than this PR. The
  // pragmatic CSP shape is: keep nonce-based ``style-src`` (so the
  // Next.js framework styles continue to pass), and allow inline
  // attributes via a dedicated ``style-src-attr 'unsafe-inline'``.
  // This is materially safer than the pre-PR baseline that allowed
  // arbitrary ``<style>`` elements via global ``'unsafe-inline'``.
  const styleSrc = isDev
    ? `style-src 'self' 'nonce-${nonce}' 'unsafe-inline' https://fonts.googleapis.com`
    : `style-src 'self' 'nonce-${nonce}' https://fonts.googleapis.com`;
  return [
    "default-src 'self'",
    scriptSrc,
    styleSrc,
    "style-src-attr 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data: https://fonts.gstatic.com",
    `connect-src 'self'${apiOrigin() ? " " + apiOrigin() : ""}${isDev ? " ws: wss:" : ""}`,
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
    "upgrade-insecure-requests",
  ].join("; ");
}

/**
 * No-nonce fallback CSP used by ``next.config.ts`` ``headers()`` for
 * routes that never transit ``proxy.ts``. Production drops
 * ``'unsafe-inline'`` from script-src (closing the ZAP Medium); the
 * proxy's per-request CSP overrides this on every request that
 * actually reaches the app, so this constant exists only as a
 * defense-in-depth default.
 *
 * Style-src keeps ``'unsafe-inline'`` here because without a nonce
 * there's no way for Next.js's runtime style emissions to execute.
 * The proxy's per-request CSP overrides this with a nonce-based
 * style-src in normal operation.
 */
export const cspDirectives = [
  "default-src 'self'",
  `script-src 'self'${isDev ? " 'unsafe-inline' 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
  "style-src-attr 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self' data: https://fonts.gstatic.com",
  `connect-src 'self'${apiOrigin() ? " " + apiOrigin() : ""}${isDev ? " ws: wss:" : ""}`,
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "object-src 'none'",
  "upgrade-insecure-requests",
].join("; ");

/**
 * The Next.js ``headers()`` config shape: ``{ key, value }[]``.
 * Used by ``next.config.ts``.
 */
export const securityHeaders: { key: string; value: string }[] = [
  { key: "Content-Security-Policy", value: cspDirectives },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  {
    key: "Permissions-Policy",
    value: "camera=(), microphone=(), geolocation=(), interest-cohort=()",
  },
  {
    key: "Strict-Transport-Security",
    value: "max-age=63072000; includeSubDomains; preload",
  },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
  { key: "Cross-Origin-Resource-Policy", value: "same-origin" },
];

/**
 * Same pack as ``securityHeaders`` but with the per-request CSP
 * (nonce-bearing) injected. ``proxy.ts`` calls this with a fresh
 * nonce on every request and stamps the result on the response.
 */
export function securityHeadersTuplesWithNonce(
  nonce: string,
): [string, string][] {
  return securityHeaders.map(({ key, value }) => {
    if (key === "Content-Security-Policy") {
      return [key, buildCspDirectives(nonce)];
    }
    return [key, value];
  });
}

/**
 * Same pack as ``securityHeaders``, exposed as ``[name, value]``
 * tuples for ergonomic use against the Web Headers API (e.g., inside
 * ``proxy.ts`` for stamping onto a ``NextResponse``). No-nonce
 * variant retained as a fallback; production callers should prefer
 * ``securityHeadersTuplesWithNonce``.
 */
export const securityHeadersTuples: [string, string][] = securityHeaders.map(
  ({ key, value }) => [key, value],
);
