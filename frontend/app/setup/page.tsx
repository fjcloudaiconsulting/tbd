"use client";

import { FormEvent, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/components/auth/AuthProvider";

export default function SetupPage() {
  const { needsSetup, loading, register, login } = useAuth();
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [password2, setPassword2] = useState("");
  const [orgName, setOrgName] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!loading && !needsSetup) {
      router.replace("/");
    }
  }, [loading, needsSetup, router]);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");

    if (password !== password2) {
      setError("Passwords do not match");
      return;
    }

    setSubmitting(true);
    try {
      await register(username, email, password, orgName || undefined);
      await login(username, password);
      router.push("/dashboard");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Setup failed");
    } finally {
      setSubmitting(false);
    }
  }

  if (loading || !needsSetup) {
    return (
      <div className="flex h-screen items-center justify-center bg-bg">
        <div className="h-6 w-6 animate-spin rounded-full border-2 border-border border-t-accent" />
      </div>
    );
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-md">
        <div className="mb-10 text-center">
          <h1 className="font-display text-3xl text-accent">Welcome to PFV2</h1>
          <p className="mt-2 text-sm text-text-secondary">
            Create your administrator account to get started.
          </p>
        </div>

        <div className="rounded-lg border border-border bg-surface p-6">
          <form onSubmit={handleSubmit} className="space-y-5">
            {error && (
              <div className="rounded-md bg-danger-dim px-4 py-3 text-sm text-danger">
                {error}
              </div>
            )}

            <div>
              <label htmlFor="username" className="mb-1.5 block text-xs font-medium uppercase tracking-wider text-text-muted">
                Username
              </label>
              <input
                id="username"
                type="text"
                required
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                className="w-full rounded-md border border-border bg-surface-raised px-4 py-2.5 text-sm text-text-primary placeholder:text-text-muted focus:border-accent focus:outline-none"
                autoComplete="username"
                autoFocus
              />
            </div>

            <div>
              <label htmlFor="email" className="mb-1.5 block text-xs font-medium uppercase tracking-wider text-text-muted">
                Email
              </label>
              <input
                id="email"
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="w-full rounded-md border border-border bg-surface-raised px-4 py-2.5 text-sm text-text-primary placeholder:text-text-muted focus:border-accent focus:outline-none"
                autoComplete="email"
              />
            </div>

            <div>
              <label htmlFor="orgName" className="mb-1.5 block text-xs font-medium uppercase tracking-wider text-text-muted">
                Organization Name
              </label>
              <input
                id="orgName"
                type="text"
                value={orgName}
                onChange={(e) => setOrgName(e.target.value)}
                placeholder="My Household"
                className="w-full rounded-md border border-border bg-surface-raised px-4 py-2.5 text-sm text-text-primary placeholder:text-text-muted focus:border-accent focus:outline-none"
              />
              <p className="mt-1.5 text-xs text-text-muted">
                Optional. Defaults to your username.
              </p>
            </div>

            <div>
              <label htmlFor="password" className="mb-1.5 block text-xs font-medium uppercase tracking-wider text-text-muted">
                Password
              </label>
              <input
                id="password"
                type="password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full rounded-md border border-border bg-surface-raised px-4 py-2.5 text-sm text-text-primary placeholder:text-text-muted focus:border-accent focus:outline-none"
                autoComplete="new-password"
              />
            </div>

            <div>
              <label htmlFor="password2" className="mb-1.5 block text-xs font-medium uppercase tracking-wider text-text-muted">
                Confirm Password
              </label>
              <input
                id="password2"
                type="password"
                required
                value={password2}
                onChange={(e) => setPassword2(e.target.value)}
                className="w-full rounded-md border border-border bg-surface-raised px-4 py-2.5 text-sm text-text-primary placeholder:text-text-muted focus:border-accent focus:outline-none"
                autoComplete="new-password"
              />
            </div>

            <button
              type="submit"
              disabled={submitting}
              className="w-full rounded-md bg-accent px-4 py-2.5 text-sm font-medium text-accent-text hover:bg-accent-hover disabled:opacity-50"
            >
              {submitting ? "Setting up..." : "Create Admin Account"}
            </button>
          </form>
        </div>

        <p className="mt-4 text-center text-xs text-text-muted">
          This account will have full administrator privileges.
        </p>
      </div>
    </div>
  );
}
