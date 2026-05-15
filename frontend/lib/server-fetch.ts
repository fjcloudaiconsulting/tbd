import "server-only";

import { logger } from "./logger";

// Sanctioned chokepoint for server-side (RSC, server-only library, server
// action) backend fetches. Every server surface MUST use this helper rather
// than calling `fetch` directly; the convention test at
// `frontend/tests/convention/rsc-fetch-guards.test.ts` enforces it in CI.
//
// Why a chokepoint:
//   - Unhandled rejected fetch or thrown JSON parse during RSC render
//     surfaces as the Next.js error boundary with an opaque digest
//     (`Reference: <digest>`). #282 is the prior incident this class of bug
//     produced. The helper returns `null` instead, so callers can redirect
//     to /login or fall back to a graceful empty state.
//   - The sanitized log payload is bounded by construction (see invariants
//     below). Direct callers tend to log `err`, which on a fetch failure
//     can contain request headers including cookies and bearer tokens.
//
// URL resolution mirrors `lib/auth-server.ts`. The browser uses relative
// URLs proxied by nginx; the server needs an absolute URL. In dev compose
// and prod the BACKEND_INTERNAL_URL env var points at the backend service;
// the fallbacks let a developer running the backend directly outside docker
// import this module and have it work.

const SERVER_API_URL =
  process.env.BACKEND_INTERNAL_URL ||
  process.env.NEXT_PUBLIC_API_URL ||
  "http://localhost:8000";

// Wrap the URL parse so a malformed SERVER_API_URL (env typo, missing
// scheme, etc.) cannot make the failure-logging path itself throw. If the
// catch block threw while building its log payload, we'd re-open the exact
// "server render hits error boundary" class this helper was meant to
// prevent. Exported for direct unit-testing.
export function safeBackendHost(): string {
  try {
    return new URL(SERVER_API_URL).host;
  } catch {
    return "invalid-backend-url";
  }
}

// Strip query strings from URL-like tokens so a thrown error message
// like 'Failed to parse URL from not a url/api?token=SECRET' cannot
// leak the secret into the structured log. Whitespace-tokenized so
// we don't over-redact prose containing question marks.
//
// Also caps the total length so a runaway error message can't blow
// up a single log line.
const MAX_ERROR_MESSAGE_LEN = 500;

function sanitizeErrorMessage(msg: string): string {
  const tokens = msg.split(/\s+/);
  const redacted = tokens.map((token) => {
    const q = token.indexOf("?");
    if (q === -1) return token;
    return token.slice(0, q) + "?[REDACTED]";
  });
  const joined = redacted.join(" ");
  if (joined.length > MAX_ERROR_MESSAGE_LEN) {
    return joined.slice(0, MAX_ERROR_MESSAGE_LEN) + "...[truncated]";
  }
  return joined;
}

export type ServerFetchOptions = {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: BodyInit;
  accessToken?: string;
  cookie?: string;
  // Allow callers to opt out of warn-level logging for specific non-OK
  // statuses that are part of normal flow (e.g. 401 from /auth/verify
  // simply means "no session", not an outage). Statuses NOT in this list
  // still emit `server_fetch_non_ok` so backend outages (500/503) are
  // never accidentally silenced.
  silentStatuses?: number[];
};

// Returns parsed JSON on success, `null` on any failure (rejected fetch,
// invalid JSON, non-OK status). Failures emit a sanitized structured
// warning via the project logger.
//
// PRIVACY INVARIANTS (must hold by construction):
//   - The catch / non-OK paths NEVER reference the request `headers`,
//     `options.cookie`, `options.accessToken`, `options.body`,
//     `res.text()`, or `res.headers`. The fields logged are bounded.
//   - `backend_host` is the host of the BACKEND URL, not the request
//     path — internal DNS info, not user-routable.
//   - `path` is the caller-provided URL path. The helper does NOT inject
//     query params, so callers MUST NOT put tokens or other secrets in
//     the path itself. Bearer tokens belong in `accessToken`.
export async function serverFetch<T>(
  path: string,
  options: ServerFetchOptions = {},
): Promise<T | null> {
  const headers: Record<string, string> = {};
  if (options.accessToken) {
    headers["Authorization"] = `Bearer ${options.accessToken}`;
  }
  if (options.cookie) {
    headers["Cookie"] = options.cookie;
  }
  if (options.body && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }

  try {
    const res = await fetch(`${SERVER_API_URL}${path}`, {
      method: options.method ?? "GET",
      headers,
      body: options.body,
      cache: "no-store",
    });

    if (!res.ok) {
      if (!options.silentStatuses?.includes(res.status)) {
        logger.warn("server_fetch_non_ok", {
          backend_host: safeBackendHost(),
          method: options.method ?? "GET",
          path: path.split("?")[0],
          status: res.status,
        });
      }
      return null;
    }

    return (await res.json()) as T;
  } catch (err) {
    const errorName = err instanceof Error ? err.name : "Unknown";
    const rawMessage = err instanceof Error ? err.message : String(err);
    logger.warn("server_fetch_failed", {
      backend_host: safeBackendHost(),
      method: options.method ?? "GET",
      path: path.split("?")[0],
      error_name: errorName,
      error_message: sanitizeErrorMessage(rawMessage),
    });
    return null;
  }
}
