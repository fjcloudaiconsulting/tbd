import { NextRequest, NextResponse } from "next/server";

/**
 * Next.js middleware — logs every request in structured JSON format
 * matching the backend/nginx log style for unified observability.
 *
 * Sensitive query parameters (tokens, codes) are stripped from logs.
 */

const SENSITIVE_PARAMS = new Set([
  "token", "code", "access_token", "refresh_token", "key", "secret", "password",
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

export function middleware(request: NextRequest) {
  const response = NextResponse.next();

  const entry = {
    timestamp: new Date().toISOString(),
    level: "info",
    logger: "frontend.access",
    method: request.method,
    path: request.nextUrl.pathname,
    query: sanitizeQuery(request.nextUrl.search),
  };

  console.log(JSON.stringify(entry));

  return response;
}

export const config = {
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|icon.svg|.*\\.(?:png|jpg|jpeg|gif|webp|svg|ico)$).*)",
  ],
};
