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

// 2026-05-18 session-stability helpers — shared between restore() (mount)
// and fetchMe() (interactive login, SSO callback, invite accept, settings
// pages via refreshMe). Both paths need the same retry budget and the
// same terminal-vs-transient discrimination so a cold-start /auth/me
// blip never lands the user at /login with a valid access token still
// in memory.

const isTransientAuthError = (err: unknown): boolean => {
  if (err instanceof ApiTimeoutError) return true;
  if (err instanceof ApiResponseError) {
    // 401/403 = terminal (real session-dead signal). Everything else
    // (5xx, 503 refresh_transient, 0 network) is worth a retry on
    // cold start.
    return err.status === 0 || err.status >= 500;
  }
  // TypeError on fetch (DNS, offline) lands here.
  return true;
};

const isTerminalAuthError = (err: unknown): boolean =>
  err instanceof ApiResponseError
  && (err.status === 401 || err.status === 403);

async function withAuthRetry<T>(fn: () => Promise<T>): Promise<T> {
  // 3 attempts; backoff 250ms, 500ms. Matches apiFetch's
  // REFRESH_TRANSIENT_RETRIES budget so the recovery story is
  // consistent across the silent-refresh path, the mount path,
  // and every interactive-login current-user load.
  const delays = [0, 250, 500];
  let lastErr: unknown;
  for (const delay of delays) {
    if (delay) await new Promise((r) => setTimeout(r, delay));
    try {
      return await fn();
    } catch (err) {
      lastErr = err;
      if (!isTransientAuthError(err)) throw err;
    }
  }
  throw lastErr;
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
    captchaToken?: string,
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
    // 2026-05-18 review fix: fetchMe is the shared current-user load
    // for interactive login, SSO callback, invite accept, and the
    // settings pages (via the `refreshMe` alias). Previously a
    // transient /auth/me failure silently set user=null and resolved,
    // which let the login flow push /dashboard with no user object;
    // AppShell then bounced back to /login with a perfectly valid
    // access token still in memory.
    //
    // After this fix:
    //   * Transient (timeout / 5xx / network) is retried 3× through
    //     withAuthRetry; if it eventually succeeds, user is set.
    //   * Persistent transient leaves user AND accessToken untouched
    //     and rethrows so the caller (login / SSO / invite / setting
    //     handler) knows to NOT proceed with a happy-path redirect.
    //     The caller's own try/catch surfaces an error to the UI; the
    //     user stays on the current screen and can retry.
    //   * Terminal 401/403 clears in-memory state (real logout) AND
    //     rethrows so the caller still aborts its happy-path flow;
    //     AppShell's redirect-on-mount handles routing to /login.
    try {
      const u = await withAuthRetry(() => apiFetch<User>("/api/v1/auth/me"));
      setUser(u);
    } catch (err) {
      if (isTerminalAuthError(err)) {
        setUser(null);
        setAccessToken(null);
      }
      // Persistent transient: state untouched. Rethrow so the caller
      // can react (e.g. login() rejects → LoginPageBody.catch shows
      // an error → router.push("/dashboard") never fires).
      throw err;
    }
  }, []);

  useEffect(() => {
    // Cold-start transient errors during restore (status timed out,
    // refresh hit a 5xx, /me network blip) used to drop the user
    // straight to /login. Calls go through the shared withAuthRetry
    // helper hoisted to module scope so restore() and fetchMe() share
    // exactly one retry / classification contract.
    const restore = async () => {
      try {
        // Check if system needs initial setup
        const status = await withAuthRetry(() =>
          apiFetch<{ needs_setup: boolean }>("/api/v1/auth/status"),
        );
        if (status.needs_setup) {
          setNeedsSetup(true);
          setLoading(false);
          return;
        }

        // Try silent refresh to restore session
        const data = await withAuthRetry(() =>
          apiFetch<TokenResponse>("/api/v1/auth/refresh", {
            method: "POST",
          }),
        );
        setAccessToken(data.access_token);

        // Load the user object with the same retry budget. Inlined
        // (rather than calling fetchMe()) because restore needs the
        // success/terminal/transient outcomes to drive its own
        // loading-state contract, which differs from fetchMe's
        // throw-on-failure contract.
        const me = await withAuthRetry(() =>
          apiFetch<User>("/api/v1/auth/me"),
        );
        setUser(me);
        setLoading(false);
      } catch (err) {
        if (isTerminalAuthError(err)) {
          // Real logout signal: clear in-memory state and let
          // AppShell's `!loading && !user` redirect to /login fire.
          setAccessToken(null);
          setUser(null);
          setLoading(false);
        } else {
          // Persistent transient (timeout / 5xx / network exhausted
          // through the retry budget). The access token may still be
          // valid; clearing it would force a spurious silent refresh
          // on next interaction AND, more importantly, dropping
          // loading=false here would let AppShell redirect to /login
          // even though the session is healthy. Keep loading=true
          // so the user sees the AppShell spinner and can reload to
          // retry; the next mount runs restore() afresh against a
          // (probably) recovered backend.
        }
      }
    };
    restore();
  }, []);

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
    captchaToken?: string,
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
        captcha_token: captchaToken || undefined,
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
