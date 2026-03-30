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
  login: (username: string, password: string) => Promise<void>;
  register: (
    username: string,
    email: string,
    password: string,
    orgName?: string
  ) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchMe = useCallback(async () => {
    try {
      const u = await apiFetch<User>("/api/auth/me");
      setUser(u);
    } catch {
      setUser(null);
      setAccessToken(null);
    }
  }, []);

  // On mount, try silent refresh to restore session
  useEffect(() => {
    const restore = async () => {
      try {
        const data = await apiFetch<TokenResponse>("/api/auth/refresh", {
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

  const login = async (username: string, password: string) => {
    const data = await apiFetch<TokenResponse>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    setAccessToken(data.access_token);
    await fetchMe();
  };

  const register = async (
    username: string,
    email: string,
    password: string,
    orgName?: string
  ) => {
    await apiFetch<User>("/api/auth/register", {
      method: "POST",
      body: JSON.stringify({
        username,
        email,
        password,
        org_name: orgName || undefined,
      }),
    });
  };

  const logout = async () => {
    try {
      await apiFetch("/api/auth/logout", { method: "POST" });
    } catch {
      // Best-effort
    }
    setAccessToken(null);
    setUser(null);
  };

  return (
    <AuthContext.Provider value={{ user, loading, login, register, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be inside AuthProvider");
  return ctx;
}
