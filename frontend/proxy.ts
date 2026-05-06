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

export function proxy(request: NextRequest) {
  const response = NextResponse.next();

  const entry = {
    timestamp: new Date().toISOString(),
    level: "info",
    logger: "frontend.access",
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
