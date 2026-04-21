"use client";

import { FormEvent, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/components/auth/AuthProvider";
import ThemeToggle from "@/components/ui/ThemeToggle";
import { input, label, btnPrimary, error as errorCls } from "@/lib/styles";
import {
  USERNAME_MAX_LENGTH,
  USERNAME_MIN_LENGTH,
  USERNAME_PATTERN,
  USERNAME_RULE_HINT,
} from "@/lib/validation";

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
    if (!loading && !needsSetup) router.replace("/");
  }, [loading, needsSetup, router]);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    if (password !== password2) { setError("Passwords do not match"); return; }
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
      <div className="flex h-screen items-center justify-center">
        <div className="h-6 w-6 animate-spin rounded-full border-2 border-border border-t-accent" />
      </div>
    );
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center px-4">
      <ThemeToggle className="absolute right-6 top-6" />

      <div className="w-full max-w-md">
        <div className="mb-10 text-center">
          <h1 className="font-display text-3xl font-semibold text-text-primary">Welcome to PFV2</h1>
          <p className="mt-2 text-sm text-text-secondary">Create your administrator account to get started.</p>
        </div>
        <div className="rounded-lg border border-border bg-surface p-6">
          <form onSubmit={handleSubmit} className="space-y-5">
            {error && <div className={errorCls}>{error}</div>}
            <div>
              <label htmlFor="setup-username" className={label}>Username</label>
              <input
                id="setup-username"
                type="text"
                required
                minLength={USERNAME_MIN_LENGTH}
                maxLength={USERNAME_MAX_LENGTH}
                pattern={USERNAME_PATTERN}
                title={USERNAME_RULE_HINT}
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                className={input}
                autoComplete="username"
                autoFocus
              />
              <p className="mt-1 text-xs text-text-muted">{USERNAME_RULE_HINT}</p>
            </div>
            <div>
              <label htmlFor="setup-email" className={label}>Email</label>
              <input id="setup-email" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} className={input} autoComplete="email" />
            </div>
            <div>
              <label htmlFor="setup-org" className={label}>Organization Name</label>
              <input id="setup-org" type="text" value={orgName} onChange={(e) => setOrgName(e.target.value)} placeholder="My Household" className={input} />
              <p className="mt-1.5 text-xs text-text-muted">Optional. Defaults to your username.</p>
            </div>
            <div>
              <label htmlFor="setup-password" className={label}>Password</label>
              <input id="setup-password" type="password" required value={password} onChange={(e) => setPassword(e.target.value)} className={input} autoComplete="new-password" />
            </div>
            <div>
              <label htmlFor="setup-password2" className={label}>Confirm Password</label>
              <input id="setup-password2" type="password" required value={password2} onChange={(e) => setPassword2(e.target.value)} className={input} autoComplete="new-password" />
            </div>
            <button type="submit" disabled={submitting} className={`w-full ${btnPrimary}`}>
              {submitting ? "Setting up..." : "Create Admin Account"}
            </button>
          </form>
        </div>
        <p className="mt-4 text-center text-xs text-text-muted">This account will have full administrator privileges.</p>
      </div>
    </div>
  );
}
