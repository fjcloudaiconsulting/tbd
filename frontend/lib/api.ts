const API_URL = process.env.NEXT_PUBLIC_API_URL || "";
const DEFAULT_TIMEOUT_MS = 10_000;
// Auth recovery paths (/auth/refresh, /auth/me, /auth/status) get a
// longer budget so the first request after a hibernated DO App Platform
// basic-xxs backend doesn't false-positive during TLS handshake + cold
// container boot. Applied only to the recovery paths; all other
// endpoints keep the 10s default.
//
// 2026-05-18 idle-recovery: bumped 25s → 45s after the production log
// (deployment 7506c8ff at 10:32:55) showed /auth/refresh resolving at
// 28s in the cold-start tail — the previous 25s budget aborted the
// fetch right when the backend was about to send the response,
// producing an orphaned 200 in the access log AND a frontend
// "refresh_transient" 503 whose retry budget then had to recover.
// The dashboard's silent .catch(() => {}) mount-loaders meant the
// retry succeeded silently but the original /accounts and /categories
// requests were never replayed visibly to the user. 45s gives the
// observed tail enough headroom that a single attempt succeeds.
const RECOVERY_TIMEOUT_MS = 45_000;
// Back-compat alias retained for the rest of the module; existing callers
// reference API_FETCH_TIMEOUT_MS in inline comments.
const API_FETCH_TIMEOUT_MS = DEFAULT_TIMEOUT_MS;
// Retry budget for transient outcomes on POST /api/v1/auth/refresh. Two
// retries with 250ms exponential backoff (250ms, 500ms). Terminal 401/403
// is NOT retried -- those mean the refresh cookie is dead, not in transit.
const REFRESH_TRANSIENT_RETRIES = 2;
const REFRESH_BACKOFF_BASE_MS = 250;

// Detect recovery paths by substring so a future ``/api/v2/auth/refresh``
// or a relative ``/auth/me`` still picks up the longer budget. Robust
// against the API_URL prefix and against future path renames.
//
// Architect P1 on PR #309: ``/auth/status`` is the FIRST endpoint
// AuthProvider hits on mount (see components/auth/AuthProvider.tsx).
// If that call times out at 10s on a cold container, the restore
// flow never reaches ``/auth/refresh`` and the user gets surfaced a
// generic 503 from the unauth path instead of the recovery one.
// Treating ``/auth/status`` as a recovery path means the cold-start
// chain (status → refresh → me) all runs on the 25s budget.
function isRecoveryPath(path: string): boolean {
  return (
    path.includes("/auth/refresh")
    || path.includes("/auth/me")
    || path.includes("/auth/status")
  );
}

function timeoutForPath(path: string): number {
  return isRecoveryPath(path) ? RECOVERY_TIMEOUT_MS : DEFAULT_TIMEOUT_MS;
}

let accessToken: string | null = null;
// Discriminated result so callers can distinguish terminal auth death
// from transient refresh failure. Architect-locked 2026-05-15.
type RefreshResult =
  | { ok: true; accessToken: string }
  | { ok: false; kind: "terminal"; status: number }
  | { ok: false; kind: "transient"; error: Error; status?: number };

// 2026-05-18 idle-recovery observability hooks. apiFetch fires
// CustomEvents on window for every refresh attempt and every
// retry-after-refresh. Lightweight — no React, no Suspense, no
// third-party metrics SDK. Gated on ``typeof window !== "undefined"``
// so SSR / vitest jsdom without window doesn't NPE.
//
// Subscribers today:
//   - tests/api/recovery-timeout.test.ts (regression tests pin the
//     end-to-end contract: 28s tail → /refresh ok → original replays)
//   - tests/lib/api.test.ts (singleflight + retry budget pins)
//   - components/AppShell.tsx pipes them into the structured JSON
//     logger, which today emits to the BROWSER console only. App
//     Platform's log shipper captures backend stdout/stderr, NOT
//     browser console output, so these events do not yet reach
//     production logs. A follow-up will wire a real client-telemetry
//     sink (POST to a backend collector, batched, rate-limited,
//     PII-redacted at source per the redaction notes below).
//
// PII contract: the ``path`` field on RetryAfterRefreshDetail is
// stripped of query string + fragment BEFORE dispatch (see the
// retry-after-refresh dispatch site for the rationale).
export interface RefreshAttemptDetail {
  attempt: number;          // 1-indexed: 1 = initial, 2 = 1st retry, 3 = 2nd retry
  outcome: "ok" | "terminal" | "transient";
  status?: number;          // HTTP status when known (terminal always; transient sometimes)
  durationMs: number;       // Wall-clock elapsed for THIS attempt
}

export interface RetryAfterRefreshDetail {
  /**
   * Pathname of the original 401-ing request. Query string and
   * fragment are stripped at dispatch time so the event detail
   * never carries user-entered values (e.g. transaction search
   * terms). Subscribers receive the route signature only.
   */
  path: string;
  status: number;           // Final response status after the retry
  ok: boolean;              // Convenience: status in [200, 300)
  durationMs: number;       // Wall-clock elapsed for the retry fetch
}

function dispatchAuthEvent<T>(name: string, detail: T): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(name, { detail }));
}

let refreshPromise: Promise<RefreshResult> | null = null;
// Count of awaiters currently holding the in-flight refreshPromise. Used
// to close the singleflight microtask gap: the original code cleared
// refreshPromise inside .finally(), which runs BEFORE awaiting callers
// resume in the next microtask, so a second 401-driven apiFetch could
// see null and fire a duplicate /refresh. We now keep refreshPromise
// non-null until every awaiter has consumed it (count returns to 0).
let refreshAwaiters = 0;
// /me probe singleflight. After a terminal /refresh, we run one
// confirmation probe against /api/v1/auth/me to defend against the
// ambiguous-401 false-logout class. Multiple awaiters of the same
// terminal refresh share the same probe.
let mePromise: Promise<boolean> | null = null;

export function setAccessToken(token: string | null) {
  accessToken = token;
}

export function getAccessToken(): string | null {
  return accessToken;
}

export class ApiTimeoutError extends Error {
  constructor() {
    super("Request timed out. Try again.");
    this.name = "ApiTimeoutError";
  }
}

// Per-call options for apiFetch. Extends RequestInit so callers keep
// passing the same method/body/headers shape they always have. The
// optional ``timeoutMs`` lets callers override the default 10s budget
// per-request (e.g. import preview/confirm, which intentionally race a
// 10s server-side parser cap).
export type ApiFetchOptions = RequestInit & {
  timeoutMs?: number;
};

async function fetchWithTimeout(
  input: RequestInfo | URL,
  init: RequestInit = {},
  timeoutMs: number = API_FETCH_TIMEOUT_MS,
): Promise<Response> {
  const controller = new AbortController();
  const upstreamSignal = init.signal;
  let timedOut = false;

  const abortFromUpstream = () => {
    controller.abort(upstreamSignal?.reason);
  };

  if (upstreamSignal?.aborted) {
    abortFromUpstream();
  } else {
    upstreamSignal?.addEventListener("abort", abortFromUpstream, {
      once: true,
    });
  }

  const timeoutId = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  try {
    return await fetch(input, {
      ...init,
      signal: controller.signal,
    });
  } catch (err) {
    if (timedOut) {
      throw new ApiTimeoutError();
    }
    throw err;
  } finally {
    clearTimeout(timeoutId);
    upstreamSignal?.removeEventListener("abort", abortFromUpstream);
  }
}

async function refreshAccessTokenOnce(attempt: number): Promise<RefreshResult> {
  const startedAt = (typeof performance !== "undefined" ? performance.now() : Date.now());
  const emit = (result: RefreshResult): RefreshResult => {
    const durationMs = (typeof performance !== "undefined" ? performance.now() : Date.now()) - startedAt;
    const detail: RefreshAttemptDetail = result.ok
      ? { attempt, outcome: "ok", durationMs }
      : result.kind === "terminal"
        ? { attempt, outcome: "terminal", status: result.status, durationMs }
        : { attempt, outcome: "transient", status: result.status, durationMs };
    dispatchAuthEvent<RefreshAttemptDetail>("auth:refresh-attempt", detail);
    return result;
  };

  let res: Response;
  try {
    // 45s recovery budget so a hibernated backend cold start with TLS
    // handshake + container boot doesn't false-positive at the
    // observed 28s tail. See RECOVERY_TIMEOUT_MS comment.
    res = await fetchWithTimeout(
      `${API_URL}/api/v1/auth/refresh`,
      {
        method: "POST",
        credentials: "include",
      },
      RECOVERY_TIMEOUT_MS,
    );
  } catch (err) {
    // ApiTimeoutError, TypeError (network), AbortError -- all transient.
    return emit({
      ok: false,
      kind: "transient",
      error: err instanceof Error ? err : new Error(String(err)),
    });
  }

  if (res.status === 401 || res.status === 403) {
    return emit({ ok: false, kind: "terminal", status: res.status });
  }

  if (!res.ok) {
    return emit({
      ok: false,
      kind: "transient",
      error: new Error(`refresh returned ${res.status}`),
      status: res.status,
    });
  }

  let data: { access_token?: string };
  try {
    data = await res.json();
  } catch (err) {
    return emit({
      ok: false,
      kind: "transient",
      error: err instanceof Error ? err : new Error("invalid JSON"),
    });
  }

  if (!data.access_token) {
    // 200 OK but no access_token: protocol failure, not auth death.
    return emit({
      ok: false,
      kind: "transient",
      error: new Error("refresh succeeded without access_token"),
    });
  }

  accessToken = data.access_token;
  return emit({ ok: true, accessToken: data.access_token });
}

// Retry budget wrapper. Re-runs refreshAccessTokenOnce up to
// REFRESH_TRANSIENT_RETRIES times when the outcome is transient
// (network/timeout/5xx/JSON parse/protocol). Terminal 401/403 short-
// circuits immediately -- those mean the refresh cookie is dead.
async function refreshAccessToken(): Promise<RefreshResult> {
  let last: RefreshResult = await refreshAccessTokenOnce(1);
  for (let attempt = 1; attempt <= REFRESH_TRANSIENT_RETRIES; attempt++) {
    if (last.ok || last.kind === "terminal") return last;
    // Exponential backoff: 250ms, 500ms.
    const delay = REFRESH_BACKOFF_BASE_MS * 2 ** (attempt - 1);
    await new Promise<void>((resolve) => setTimeout(resolve, delay));
    last = await refreshAccessTokenOnce(attempt + 1);
  }
  return last;
}

// One-shot /api/v1/auth/me probe used to disambiguate a terminal /refresh
// response. If the access token is still in memory and /me returns 200,
// the session is alive (the /refresh 401 was a backend race / partial
// outage), so we must NOT dispatch auth:unauthenticated. If /me also
// fails terminally we proceed with logout. Anything else (network/timeout
// /5xx) is treated as "cannot confirm" => preserve current behavior and
// proceed with logout. This is safer than leaving the user wedged.
async function probeAuthMeAlive(): Promise<boolean> {
  if (!accessToken) return false;
  try {
    // 25s recovery budget so a cold backend doesn't trip a false
    // logout when /me confirms session liveness after a transient
    // refresh failure.
    const res = await fetchWithTimeout(
      `${API_URL}/api/v1/auth/me`,
      {
        method: "GET",
        credentials: "include",
        headers: { Authorization: `Bearer ${accessToken}` },
      },
      RECOVERY_TIMEOUT_MS,
    );
    return res.status === 200;
  } catch {
    return false;
  }
}

export async function apiFetch<T>(
  path: string,
  options: ApiFetchOptions = {}
): Promise<T> {
  // Pull timeoutMs out of options BEFORE passing the rest to native fetch
  // so it doesn't pollute the RequestInit. The same caller-provided value
  // applies to both the primary request and the retry-after-refresh.
  const { timeoutMs, ...fetchInit } = options;
  // Path-specific default: recovery routes get 25s, everything else 10s.
  // An explicit per-call timeoutMs override always wins. Same effective
  // budget is reused for the retry-after-refresh below.
  const effectiveTimeout = timeoutMs ?? timeoutForPath(path);
  const headers = new Headers(fetchInit.headers);

  if (accessToken) {
    headers.set("Authorization", `Bearer ${accessToken}`);
  }

  if (
    fetchInit.body &&
    typeof fetchInit.body === "string" &&
    !headers.has("Content-Type")
  ) {
    headers.set("Content-Type", "application/json");
  }

  let res = await fetchWithTimeout(
    `${API_URL}${path}`,
    {
      ...fetchInit,
      headers,
      credentials: "include",
    },
    effectiveTimeout,
  );

  // On 401, attempt one silent refresh (even without a current token --
  // the refresh cookie may still be valid). Credential-check endpoints
  // skip the entire silent-refresh flow: a 401 on /login or /mfa/verify
  // means bad input from the caller, not an expired session, so we just
  // surface the 401 below without touching refresh/probe/event.
  const isCredCheck =
    path.startsWith("/api/v1/auth/login") ||
    path.startsWith("/api/v1/auth/mfa/verify");
  if (res.status === 401 && !isCredCheck) {
    // Singleflight: every concurrent 401-driven caller shares one
    // refreshPromise. We increment refreshAwaiters BEFORE awaiting and
    // only clear refreshPromise once the last awaiter has consumed it.
    // This closes the microtask gap where the old `.finally(null)` ran
    // before queued awaiters resumed, leaking duplicate /refresh calls.
    if (!refreshPromise) {
      refreshPromise = refreshAccessToken();
    }
    const sharedPromise = refreshPromise;
    refreshAwaiters++;
    let refreshResult: RefreshResult;
    try {
      refreshResult = await sharedPromise;
    } finally {
      refreshAwaiters--;
      if (refreshAwaiters === 0 && refreshPromise === sharedPromise) {
        refreshPromise = null;
      }
    }

    if (refreshResult.ok) {
      headers.set("Authorization", `Bearer ${refreshResult.accessToken}`);
      const retryStartedAt = (typeof performance !== "undefined" ? performance.now() : Date.now());
      res = await fetchWithTimeout(
        `${API_URL}${path}`,
        {
          ...fetchInit,
          headers,
          credentials: "include",
        },
        effectiveTimeout,
      );
      // 2026-05-18 idle-recovery observability hook. Subscribers
      // (today: AppShell's browser-console logger; tomorrow: a real
      // client-telemetry sink) can confirm the singleflight handed
      // the new bearer to the original 401'd request AND the retry
      // actually completed. Without this, a page-level silent
      // ``.catch(() => {})`` on the original fetcher would mask
      // retry failures entirely.
      //
      // PII redaction: ``path`` as passed in by callers can include
      // user-entered values in the query string (e.g.
      // ``/api/v1/transactions?q=mortgage`` carries a search term
      // entered into the transactions filter). Strip the query
      // string (and any fragment) BEFORE dispatching so the event
      // detail never exposes user input to telemetry consumers.
      // Pathname alone is the route signature — enough for ops
      // triage, none of the PII surface.
      const safePath = path.split("?")[0].split("#")[0];
      const retryDurationMs = (typeof performance !== "undefined" ? performance.now() : Date.now()) - retryStartedAt;
      dispatchAuthEvent<RetryAfterRefreshDetail>("auth:retry-after-refresh", {
        path: safePath,
        status: res.status,
        ok: res.ok,
        durationMs: retryDurationMs,
      });
      // Retry attempted; fall through to normal !res.ok handling below.
    } else if (refreshResult.kind === "terminal") {
      // Ambiguous-401 defense: before dispatching auth:unauthenticated,
      // probe /api/v1/auth/me once. If it returns 200 the access token
      // is still valid and the /refresh 401 was a backend race or
      // partial outage, NOT auth death. In that case we leave the
      // session intact and let the caller see the original 401 so SWR
      // can run its normal retry. All concurrent callers share one
      // probe via mePromise singleflight.
      if (!mePromise) {
        mePromise = probeAuthMeAlive().finally(() => {
          // /me probe completes once per terminal-refresh batch; clear
          // immediately so the NEXT terminal /refresh re-probes fresh.
          mePromise = null;
        });
      }
      const sessionAlive = await mePromise;
      if (!sessionAlive) {
        // True auth death. Clear in-memory token and notify
        // AuthProvider so AppShell can redirect the user to /login.
        // Dispatch is gated on accessToken !== null so a herd of
        // concurrent 401 callers only emits one event: whichever
        // awaiter clears the token first wins; subsequent awaiters
        // observe accessToken === null and skip the duplicate event.
        if (accessToken !== null) {
          accessToken = null;
          if (typeof window !== "undefined") {
            window.dispatchEvent(new Event("auth:unauthenticated"));
          }
        }
      }
      // Fall through: caller sees the original 401 ApiResponseError.
      // When sessionAlive is true, the session stays intact and SWR
      // can retry through its normal path.
    } else {
      // refreshResult.kind === "transient": refresh cookie may still be
      // valid; do NOT clear auth state. Throw a recoverable error so SWR
      // / the caller can show a retry path. User stays in-app.
      throw new ApiResponseError(
        503,
        "Session refresh temporarily unavailable. Please try again.",
        "refresh_transient",
        refreshResult.error.message,
      );
    }
  }

  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    let message: string;
    let code: string | undefined;
    if (Array.isArray(body.detail)) {
      // FastAPI 422 validation error: detail is a list of
      // { loc, msg, type, ... } objects. Flatten to "field: message"
      // per entry so users see something useful instead of
      // "[object Object]".
      message = body.detail
        .map((e: { loc?: unknown[]; msg?: string }) => {
          const field = Array.isArray(e.loc)
            ? e.loc.filter((p) => p !== "body" && typeof p === "string").join(".")
            : "";
          const msg = e.msg ?? "Invalid input";
          return field ? `${field}: ${msg}` : msg;
        })
        .join("; ");
    } else if (typeof body.detail === "string") {
      message = body.detail;
    } else if (
      body.detail &&
      typeof body.detail === "object" &&
      typeof (body.detail as { message?: unknown }).message === "string"
    ) {
      // Structured error: backend returns { detail: { code, message } }.
      // Used for the L1.8 email-verified gate so the login screen can
      // distinguish unverified from deactivated without string matching.
      const d = body.detail as { code?: unknown; message: string };
      message = d.message;
      if (typeof d.code === "string") code = d.code;
    } else {
      message = "Request failed";
    }
    throw new ApiResponseError(res.status, message, code, body.detail);
  }

  // 204 No Content
  if (res.status === 204) {
    return undefined as unknown as T;
  }

  return res.json();
}

export function extractErrorMessage(err: unknown, fallback = "Failed"): string {
  return err instanceof Error ? err.message : fallback;
}

export class ApiResponseError extends Error {
  constructor(
    public status: number,
    message: string,
    public code?: string,
    public detail?: unknown
  ) {
    super(message);
    this.name = "ApiResponseError";
  }
}
