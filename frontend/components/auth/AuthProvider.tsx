"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react";
import { apiFetch, setAccessToken } from "@/lib/api";
import type { User, TokenResponse } from "@/lib/types";

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
    } catch {
      setUser(null);
      setAccessToken(null);
    }
  }, []);

  useEffect(() => {
    const restore = async () => {
      try {
        // Check if system needs initial setup
        const status = await apiFetch<{ needs_setup: boolean }>(
          "/api/v1/auth/status"
        );
        if (status.needs_setup) {
          setNeedsSetup(true);
          setLoading(false);
          return;
        }

        // Try silent refresh to restore session
        const data = await apiFetch<TokenResponse>("/api/v1/auth/refresh", {
          method: "POST",
        });
        setAccessToken(data.access_token);
        await fetchMe();
      } catch {
        // No valid session
      } finally {
        setLoading(false);
      }
    };
    restore();
  }, [fetchMe]);

  const login = async (loginId: string, password: string) => {
    const data = await apiFetch<TokenResponse>("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ login: loginId, password }),
    });
    setAccessToken(data.access_token);
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
