import { NextRequest, NextResponse } from "next/server";

/**
 * Next.js middleware — logs every request in structured JSON format
 * matching the backend/nginx log style for unified observability.
 */
export function middleware(request: NextRequest) {
  const start = Date.now();
  const response = NextResponse.next();

  // Log after response is prepared
  const entry = {
    timestamp: new Date().toISOString(),
    level: "info",
    logger: "frontend.access",
    method: request.method,
    path: request.nextUrl.pathname,
    query: request.nextUrl.search || undefined,
    user_agent: request.headers.get("user-agent")?.slice(0, 100),
    request_time_ms: Date.now() - start,
  };

  // Write to stdout as JSON (Edge runtime compatible)
  console.log(JSON.stringify(entry));

  return response;
}

export const config = {
  // Log page navigations, skip static assets and API proxied through nginx
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|icon.svg|.*\\.(?:png|jpg|jpeg|gif|webp|svg|ico)$).*)",
  ],
};
