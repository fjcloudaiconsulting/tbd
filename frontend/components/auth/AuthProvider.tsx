"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react";
import {
  ApiResponseError,
  ApiTimeoutError,
  apiFetch,
  setAccessToken,
} from "@/lib/api";
import type { User, TokenResponse, MfaChallengeResponse } from "@/lib/types";

export class MfaRequiredError extends Error {
  constructor(public mfaToken: string) {
    super("MFA required");
    this.name = "MfaRequiredError";
  }
}

interface AuthContextValue {
  user: User | null;
  loading: boolean;
  needsSetup: boolean;
  login: (login: string, password: string) => Promise<void>;
  register: (
    username: string,
    email: string,
    password: string,
    orgName?: string,
    firstName?: string,
    lastName?: string,
  ) => Promise<void>;
  logout: () => Promise<void>;
  refreshMe: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [needsSetup, setNeedsSetup] = useState(false);

  const fetchMe = useCallback(async () => {
    try {
      const u = await apiFetch<User>("/api/v1/auth/me");
      setUser(u);
    } catch (err) {
      // Only treat a 401/403 from /auth/me as actual logout. A
      // transient timeout, network error, or 5xx leaves accessToken
      // alone so the next interaction can retry against the same
      // valid session — clearing it would trigger a spurious silent
      // refresh and (under family-revoke semantics) needlessly rotate
      // a healthy cookie.
      if (
        err instanceof ApiResponseError
        && (err.status === 401 || err.status === 403)
      ) {
        setUser(null);
        setAccessToken(null);
      } else {
        setUser(null);
      }
    }
  }, []);

  useEffect(() => {
    // Cold-start transient errors during restore (status timed out,
    // refresh hit a 5xx, /me network blip) used to drop the user
    // straight to /login. Wrap the calls in a small retry budget
    // matched to apiFetch's own transient classifier — terminal
    // 401/403 from /auth/refresh still falls through immediately so
    // a real logged-out user is not stuck waiting.
    const isTransient = (err: unknown): boolean => {
      if (err instanceof ApiTimeoutError) return true;
      if (err instanceof ApiResponseError) {
        // 401/403 = terminal (real session-dead signal). Everything
        // else (5xx, 503 refresh_transient, 0 network) is worth a
        // retry on cold start.
        return err.status === 0 || err.status >= 500;
      }
      // TypeError on fetch (DNS, offline) lands here.
      return true;
    };

    const withRetry = async <T,>(fn: () => Promise<T>): Promise<T> => {
      // 3 attempts; backoff 250ms, 500ms. Matches apiFetch's
      // REFRESH_TRANSIENT_RETRIES budget so the recovery story is
      // consistent across the silent-refresh path and the mount path.
      const delays = [0, 250, 500];
      let lastErr: unknown;
      for (const delay of delays) {
        if (delay) await new Promise((r) => setTimeout(r, delay));
        try {
          return await fn();
        } catch (err) {
          lastErr = err;
          if (!isTransient(err)) throw err;
        }
      }
      throw lastErr;
    };

    const restore = async () => {
      try {
        // Check if system needs initial setup
        const status = await withRetry(() =>
          apiFetch<{ needs_setup: boolean }>("/api/v1/auth/status"),
        );
        if (status.needs_setup) {
          setNeedsSetup(true);
          setLoading(false);
          return;
        }

        // Try silent refresh to restore session
        const data = await withRetry(() =>
          apiFetch<TokenResponse>("/api/v1/auth/refresh", {
            method: "POST",
          }),
        );
        setAccessToken(data.access_token);
        await fetchMe();
      } catch {
        // Either no valid session (terminal 401/403) or persistent
        // transient (all retries exhausted). Either way render the
        // signed-out tree — the user can reload to retry transient.
      } finally {
        setLoading(false);
      }
    };
    restore();
  }, [fetchMe]);

  // Listen for terminal 401s dispatched by apiFetch so we clear React state
  // and AppShell can redirect the user to /login instead of spinning forever.
  useEffect(() => {
    const handler = () => {
      setUser(null);
      setAccessToken(null);
    };
    window.addEventListener("auth:unauthenticated", handler);
    return () => window.removeEventListener("auth:unauthenticated", handler);
  }, []);

  const login = async (loginId: string, password: string) => {
    const data = await apiFetch<TokenResponse | MfaChallengeResponse>("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ login: loginId, password }),
    });

    // MFA challenge — throw so the login page can redirect
    if ("mfa_required" in data && data.mfa_required) {
      throw new MfaRequiredError((data as MfaChallengeResponse).mfa_token);
    }

    const tokenData = data as TokenResponse;
    setAccessToken(tokenData.access_token);
    await fetchMe();
    setNeedsSetup(false);
  };

  const register = async (
    username: string,
    email: string,
    password: string,
    orgName?: string,
    firstName?: string,
    lastName?: string,
  ) => {
    await apiFetch<User>("/api/v1/auth/register", {
      method: "POST",
      body: JSON.stringify({
        username,
        email,
        password,
        org_name: orgName || undefined,
        first_name: firstName || undefined,
        last_name: lastName || undefined,
      }),
    });
  };

  const logout = async () => {
    try {
      await apiFetch("/api/v1/auth/logout", { method: "POST" });
    } catch {
      // Best-effort
    }
    setAccessToken(null);
    setUser(null);
  };

  return (
    <AuthContext.Provider
      value={{ user, loading, needsSetup, login, register, logout, refreshMe: fetchMe }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be inside AuthProvider");
  return ctx;
}
