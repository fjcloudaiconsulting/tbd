import { NextRequest, NextResponse } from "next/server";

/**
 * Next.js middleware — logs every request in structured JSON format
 * matching the backend/nginx log style for unified observability.
 *
 * Sensitive query parameters (tokens, codes) are stripped from logs.
 */

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

  // Forward the id to the backend (or onward to whoever the request
  // is being proxied to). The backend's RequestContextMiddleware
  // uses an inbound X-Request-Id verbatim when reasonable, so this
  // gives us end-to-end correlation across frontend → nginx → backend.
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-request-id", requestId);
  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("x-request-id", requestId);

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
