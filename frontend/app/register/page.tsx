"use client";

import { FormEvent, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { useAuth } from "@/components/auth/AuthProvider";
import ThemeToggle from "@/components/ui/ThemeToggle";
import { input, label, btnPrimary, error as errorCls } from "@/lib/styles";

export default function RegisterPage() {
  const { user, register, loading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!loading && user) router.replace("/dashboard");
  }, [loading, user, router]);

  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [password2, setPassword2] = useState("");
  const [orgName, setOrgName] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    if (password !== password2) { setError("Passwords do not match"); return; }
    setSubmitting(true);
    try {
      await register(username, email, password, orgName || undefined);
      router.push("/login");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Registration failed");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center px-4">
      <ThemeToggle className="absolute right-6 top-6" />

      <div className="w-full max-w-sm">
        <div className="mb-10 text-center">
          <h1 className="font-display text-3xl font-semibold text-text-primary">Create Account</h1>
          <p className="mt-1.5 text-sm text-text-muted">Join PFV2</p>
        </div>
        <form onSubmit={handleSubmit} className="space-y-5">
          {error && <div className={errorCls}>{error}</div>}
          <div>
            <label htmlFor="reg-username" className={label}>Username</label>
            <input id="reg-username" type="text" required value={username} onChange={(e) => setUsername(e.target.value)} className={input} autoComplete="username" />
          </div>
          <div>
            <label htmlFor="reg-email" className={label}>Email</label>
            <input id="reg-email" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} className={input} autoComplete="email" />
          </div>
          <div>
            <label htmlFor="reg-org" className={label}>Organization <span className="normal-case tracking-normal">(optional)</span></label>
            <input id="reg-org" type="text" value={orgName} onChange={(e) => setOrgName(e.target.value)} placeholder="My Household" className={input} />
          </div>
          <div>
            <label htmlFor="reg-password" className={label}>Password</label>
            <input id="reg-password" type="password" required value={password} onChange={(e) => setPassword(e.target.value)} className={input} autoComplete="new-password" />
          </div>
          <div>
            <label htmlFor="reg-password2" className={label}>Confirm Password</label>
            <input id="reg-password2" type="password" required value={password2} onChange={(e) => setPassword2(e.target.value)} className={input} autoComplete="new-password" />
          </div>
          <button type="submit" disabled={submitting} className="w-full rounded-md bg-accent px-4 py-2.5 text-sm font-medium text-accent-text hover:bg-accent-hover disabled:opacity-50">
            {submitting ? "Creating account..." : "Create Account"}
          </button>
        </form>
        <p className="mt-6 text-center text-sm text-text-muted">
          Already have an account?{" "}
          <Link href="/login" className="text-accent hover:text-accent-hover">Sign In</Link>
        </p>
      </div>
    </div>
  );
}
