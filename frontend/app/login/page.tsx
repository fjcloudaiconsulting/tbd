"use client";

import { FormEvent, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { useAuth } from "@/components/auth/AuthProvider";
import ThemeToggle from "@/components/ui/ThemeToggle";
import { apiFetch } from "@/lib/api";
import { input, label, btnPrimary, btnSecondary, error as errorCls } from "@/lib/styles";

export default function LoginPage() {
  const { user, login, loading, needsSetup } = useAuth();
  const router = useRouter();
  const [loginId, setLoginId] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!loading && needsSetup) router.replace("/setup");
    if (!loading && user) router.replace("/dashboard");
  }, [loading, needsSetup, user, router]);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setSubmitting(true);
    try {
      await login(loginId, password);
      router.push("/dashboard");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleGoogleLogin() {
    try {
      const data = await apiFetch<{ redirect_url: string }>("/api/v1/auth/google");
      window.location.href = data.redirect_url;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Google sign-in is not available");
    }
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center px-4">
      <ThemeToggle className="absolute right-6 top-6" />

      <div className="w-full max-w-sm">
        <div className="mb-10 text-center">
          <h1 className="font-display text-3xl font-semibold text-text-primary">PFV2</h1>
          <p className="mt-1.5 text-sm text-text-muted">Personal Finance</p>
        </div>
        <form onSubmit={handleSubmit} className="space-y-5">
          {error && <div className={errorCls}>{error}</div>}
          <div>
            <label htmlFor="login-id" className={label}>Email or Username</label>
            <input id="login-id" type="text" required value={loginId} onChange={(e) => setLoginId(e.target.value)} className={input} autoComplete="username" placeholder="you@example.com" />
          </div>
          <div>
            <label htmlFor="login-password" className={label}>Password</label>
            <input id="login-password" type="password" required value={password} onChange={(e) => setPassword(e.target.value)} className={input} autoComplete="current-password" />
          </div>
          <div className="text-right">
            <Link href="/forgot-password" className="text-xs text-accent hover:text-accent-hover">Forgot your password?</Link>
          </div>
          <button type="submit" disabled={submitting} className={`w-full ${btnPrimary}`}>
            {submitting ? "Signing in..." : "Sign In"}
          </button>
          <div className="flex items-center gap-3 my-4">
            <div className="flex-1 border-t border-border" />
            <span className="text-xs text-text-muted">or</span>
            <div className="flex-1 border-t border-border" />
          </div>
          <button onClick={handleGoogleLogin} className={btnSecondary + " w-full"} type="button">
            Sign in with Google
          </button>
        </form>
        <p className="mt-6 text-center text-sm text-text-muted">
          Don&apos;t have an account?{" "}
          <Link href="/register" className="text-accent hover:text-accent-hover">Register</Link>
        </p>
      </div>
    </div>
  );
}
