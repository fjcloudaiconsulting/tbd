const API_URL = process.env.NEXT_PUBLIC_API_URL || "";

let accessToken: string | null = null;
let refreshPromise: Promise<string | null> | null = null;

export function setAccessToken(token: string | null) {
  accessToken = token;
}

export function getAccessToken(): string | null {
  return accessToken;
}

async function refreshAccessToken(): Promise<string | null> {
  try {
    const res = await fetch(`${API_URL}/api/v1/auth/refresh`, {
      method: "POST",
      credentials: "include",
    });
    if (!res.ok) return null;
    const data = await res.json();
    accessToken = data.access_token;
    return accessToken;
  } catch {
    return null;
  }
}

export async function apiFetch<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const headers = new Headers(options.headers);

  if (accessToken) {
    headers.set("Authorization", `Bearer ${accessToken}`);
  }

  if (
    options.body &&
    typeof options.body === "string" &&
    !headers.has("Content-Type")
  ) {
    headers.set("Content-Type", "application/json");
  }

  let res = await fetch(`${API_URL}${path}`, {
    ...options,
    headers,
    credentials: "include",
  });

  // On 401, attempt one silent refresh (even without a current token —
  // the refresh cookie may still be valid)
  if (res.status === 401) {
    if (!refreshPromise) {
      refreshPromise = refreshAccessToken().finally(() => {
        refreshPromise = null;
      });
    }
    const newToken = await refreshPromise;

    if (newToken) {
      headers.set("Authorization", `Bearer ${newToken}`);
      res = await fetch(`${API_URL}${path}`, {
        ...options,
        headers,
        credentials: "include",
      });
    }

    // If still 401 after refresh attempt, the session is dead. Clear the
    // in-memory token and notify AuthProvider so AppShell can redirect to
    // /login. Skip for credential-check endpoints where 401 means bad input,
    // not an expired session.
    if (res.status === 401) {
      const isCredCheck =
        path.startsWith("/api/v1/auth/login") ||
        path.startsWith("/api/v1/auth/mfa/verify");
      if (!isCredCheck) {
        accessToken = null;
        if (typeof window !== "undefined") {
          window.dispatchEvent(new Event("auth:unauthenticated"));
        }
      }
    }
  }

  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    let message: string;
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
    } else {
      message = "Request failed";
    }
    throw new ApiResponseError(res.status, message);
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
    message: string
  ) {
    super(message);
    this.name = "ApiResponseError";
  }
}
