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
//   - Native `fetch` has no built-in timeout, and Next.js does not inject
//     one. When the backend wedges (e.g. Redis socket half-broken cascading
//     through the rate-limit path) an awaited fetch inside an RSC never
//     resolves, the render never completes, and the page stays on its
//     loading.tsx Suspense fallback forever. This helper bounds every
//     request with an `AbortController`-based timeout (5s default) so a
//     stuck backend can never produce an indefinite spinner. PR #288.
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

// Default bounded budget for every server-side fetch. Tighter than the
// client-side `apiFetch` 10s default (#286) because a server-side hang
// strands the RSC render and keeps `loading.tsx` mounted, which is a
// worse UX than the client surface (which can still react to user
// input). Callers can override per-call via `ServerFetchOptions.timeoutMs`.
const DEFAULT_SERVER_FETCH_TIMEOUT_MS = 5_000;

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
  // Per-call timeout budget in milliseconds. Defaults to 5s. Set this
  // tighter for hot-path verify calls or looser for known-slow reads.
  // The helper aborts the underlying fetch via AbortController when the
  // budget elapses; the failure surfaces as a null return with a
  // `server_fetch_failed` log carrying `reason: "timeout"`.
  //
  // SIDE EFFECT: passing an AbortController signal to `fetch` opts the
  // request OUT of Next.js 16's automatic fetch memoization across a
  // single render pass. Two RSCs calling the same URL through this
  // helper will issue two upstream requests instead of sharing one. For
  // this codebase the trade-off is acceptable: no two RSCs share a URL
  // today, and the "no infinite spinner" guarantee is non-negotiable.
  timeoutMs?: number;
};

// Discriminated result from `serverFetchResult`. Callers that need to
// distinguish "401 means no session" from "timeout / 5xx / network error
// means the backend is wedged" should use `serverFetchResult` and switch
// on `kind`. Most call sites just want JSON-or-null and should keep using
// the thin `serverFetch` wrapper below.
//
// `kind` taxonomy:
//   - "ok"            — 2xx, JSON parsed, payload returned in `data`.
//   - "http_error"    — non-2xx response (status carried in `status`).
//     401/403 typically mean "unauthenticated"; 5xx typically means
//     transient backend trouble; callers decide per route.
//   - "timeout"       — our AbortController fired because the budget
//     elapsed. Treat as transient.
//   - "abort"         — AbortError raised outside of our timeout (e.g.
//     a caller-supplied signal was aborted). Treat as transient.
//   - "network_error" — fetch rejected (DNS, ECONNREFUSED, TLS, etc.).
//     Treat as transient.
//   - "invalid_json"  — 2xx response but `res.json()` threw. Treat as
//     transient (backend contract drifted, partial response, etc.).
export type ServerFetchResult<T> =
  | { kind: "ok"; data: T }
  | { kind: "http_error"; status: number }
  | { kind: "timeout" }
  | { kind: "abort" }
  | { kind: "network_error" }
  | { kind: "invalid_json" };

// Returns the full discriminated result. Use this from `lib/auth-server.ts`
// and any other caller that needs to triage transient vs terminal failure
// modes. The thin `serverFetch` wrapper below discards the discriminant
// and returns `T | null` for the (still common) case where the caller
// simply wants JSON-or-fallback.
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
//   - The `signal` field on the request init is OURS — it never carries
//     a caller's AbortSignal, only the timeout controller.
export async function serverFetchResult<T>(
  path: string,
  options: ServerFetchOptions = {},
): Promise<ServerFetchResult<T>> {
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

  const timeoutMs = options.timeoutMs ?? DEFAULT_SERVER_FETCH_TIMEOUT_MS;
  const controller = new AbortController();
  let timedOut = false;
  const timeoutId = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  try {
    const res = await fetch(`${SERVER_API_URL}${path}`, {
      method: options.method ?? "GET",
      headers,
      body: options.body,
      cache: "no-store",
      signal: controller.signal,
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
      return { kind: "http_error", status: res.status };
    }

    try {
      const data = (await res.json()) as T;
      return { kind: "ok", data };
    } catch (jsonErr) {
      const errorName = jsonErr instanceof Error ? jsonErr.name : "Unknown";
      const rawMessage =
        jsonErr instanceof Error ? jsonErr.message : String(jsonErr);
      logger.warn("server_fetch_failed", {
        backend_host: safeBackendHost(),
        method: options.method ?? "GET",
        path: path.split("?")[0],
        error_name: errorName,
        error_message: sanitizeErrorMessage(rawMessage),
        reason: "invalid_json",
        timeout_ms: timeoutMs,
      });
      return { kind: "invalid_json" };
    }
  } catch (err) {
    const errorName = err instanceof Error ? err.name : "Unknown";
    const rawMessage = err instanceof Error ? err.message : String(err);
    // Timeout (AbortError raised by our own setTimeout) is its own
    // log reason so on-call can distinguish wedged-backend hangs from
    // raw network failures. Any other AbortError that didn't come from
    // our timeout is currently unreachable (we never expose our
    // controller upstream) but is taxonomically distinct.
    let reason: "timeout" | "abort" | "network_error";
    let resultKind: "timeout" | "abort" | "network_error";
    if (timedOut) {
      reason = "timeout";
      resultKind = "timeout";
    } else if (errorName === "AbortError" || errorName === "TimeoutError") {
      reason = "abort";
      resultKind = "abort";
    } else {
      reason = "network_error";
      resultKind = "network_error";
    }
    logger.warn("server_fetch_failed", {
      backend_host: safeBackendHost(),
      method: options.method ?? "GET",
      path: path.split("?")[0],
      error_name: errorName,
      error_message: sanitizeErrorMessage(rawMessage),
      reason,
      timeout_ms: timeoutMs,
    });
    return { kind: resultKind };
  } finally {
    clearTimeout(timeoutId);
  }
}

// Thin convenience wrapper: discards the discriminant and returns parsed
// JSON on success, `null` on any failure. Existing RSC callers that just
// want "data or empty fallback" should keep using this. Callers that need
// to distinguish 401 from timeout (notably `lib/auth-server.ts`) must use
// `serverFetchResult` and switch on `kind`.
export async function serverFetch<T>(
  path: string,
  options: ServerFetchOptions = {},
): Promise<T | null> {
  const result = await serverFetchResult<T>(path, options);
  return result.kind === "ok" ? result.data : null;
}
