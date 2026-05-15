const API_URL = process.env.NEXT_PUBLIC_API_URL || "";
const API_FETCH_TIMEOUT_MS = 10_000;

let accessToken: string | null = null;
// Discriminated result so callers can distinguish terminal auth death
// from transient refresh failure. Architect-locked 2026-05-15.
type RefreshResult =
  | { ok: true; accessToken: string }
  | { ok: false; kind: "terminal"; status: number }
  | { ok: false; kind: "transient"; error: Error; status?: number };

let refreshPromise: Promise<RefreshResult> | null = null;

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

async function refreshAccessToken(): Promise<RefreshResult> {
  let res: Response;
  try {
    res = await fetchWithTimeout(`${API_URL}/api/v1/auth/refresh`, {
      method: "POST",
      credentials: "include",
    });
  } catch (err) {
    // ApiTimeoutError, TypeError (network), AbortError -- all transient.
    return {
      ok: false,
      kind: "transient",
      error: err instanceof Error ? err : new Error(String(err)),
    };
  }

  if (res.status === 401 || res.status === 403) {
    return { ok: false, kind: "terminal", status: res.status };
  }

  if (!res.ok) {
    return {
      ok: false,
      kind: "transient",
      error: new Error(`refresh returned ${res.status}`),
      status: res.status,
    };
  }

  let data: { access_token?: string };
  try {
    data = await res.json();
  } catch (err) {
    return {
      ok: false,
      kind: "transient",
      error: err instanceof Error ? err : new Error("invalid JSON"),
    };
  }

  if (!data.access_token) {
    // 200 OK but no access_token: protocol failure, not auth death.
    return {
      ok: false,
      kind: "transient",
      error: new Error("refresh succeeded without access_token"),
    };
  }

  accessToken = data.access_token;
  return { ok: true, accessToken: data.access_token };
}

export async function apiFetch<T>(
  path: string,
  options: ApiFetchOptions = {}
): Promise<T> {
  // Pull timeoutMs out of options BEFORE passing the rest to native fetch
  // so it doesn't pollute the RequestInit. The same caller-provided value
  // applies to both the primary request and the retry-after-refresh.
  const { timeoutMs, ...fetchInit } = options;
  const effectiveTimeout = timeoutMs ?? API_FETCH_TIMEOUT_MS;
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
  // the refresh cookie may still be valid)
  if (res.status === 401) {
    if (!refreshPromise) {
      refreshPromise = refreshAccessToken().finally(() => {
        refreshPromise = null;
      });
    }
    const refreshResult = await refreshPromise;

    if (refreshResult.ok) {
      headers.set("Authorization", `Bearer ${refreshResult.accessToken}`);
      res = await fetchWithTimeout(
        `${API_URL}${path}`,
        {
          ...fetchInit,
          headers,
          credentials: "include",
        },
        effectiveTimeout,
      );
      // Retry attempted; fall through to normal !res.ok handling below.
    } else if (refreshResult.kind === "terminal") {
      // True auth death: refresh cookie invalid/expired. Clear in-memory
      // token and notify AuthProvider so AppShell can redirect to /login.
      // Skip for credential-check endpoints where 401 means bad input,
      // not an expired session.
      const isCredCheck =
        path.startsWith("/api/v1/auth/login") ||
        path.startsWith("/api/v1/auth/mfa/verify");
      if (!isCredCheck) {
        accessToken = null;
        if (typeof window !== "undefined") {
          window.dispatchEvent(new Event("auth:unauthenticated"));
        }
      }
      // Fall through: caller sees the original 401 ApiResponseError.
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
