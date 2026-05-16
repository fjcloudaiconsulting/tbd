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

// CSP. Tailwind + styled-jsx + Next's hydration inline scripts force
// 'unsafe-inline' on both script-src and style-src; dev also needs
// 'unsafe-eval' and WebSocket connections for Fast Refresh. Google
// Fonts (Fraunces + Outfit, loaded from app/layout.tsx) need their
// two origins allowlisted on style-src and font-src.
export const cspDirectives = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${isDev ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
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
 * Same pack as ``securityHeaders``, exposed as ``[name, value]``
 * tuples for ergonomic use against the Web Headers API (e.g., inside
 * ``proxy.ts`` for stamping onto a ``NextResponse``).
 */
export const securityHeadersTuples: [string, string][] = securityHeaders.map(
  ({ key, value }) => [key, value],
);
