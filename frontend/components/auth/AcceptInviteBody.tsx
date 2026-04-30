"use client";

import { FormEvent, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import ThemeToggle from "@/components/ui/ThemeToggle";
import Spinner from "@/components/ui/Spinner";
import { ApiResponseError, apiFetch, setAccessToken } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";
import { input, label, btnPrimary, error as errorCls } from "@/lib/styles";

type Preview = {
  org_name: string;
  email: string;
  role: "owner" | "admin" | "member";
  is_reactivation: boolean;
  existing_username?: string | null;
};

export default function AcceptInviteBody() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const token = searchParams.get("token") ?? "";
  const { refreshMe } = useAuth();

  const [preview, setPreview] = useState<Preview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitError, setSubmitError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!token) {
      setPreviewError("Missing invitation token.");
      setLoading(false);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const data = await apiFetch<Preview>(
          `/api/v1/orgs/invitations/preview?token=${encodeURIComponent(token)}`,
        );
        if (cancelled) return;
        setPreview(data);
        if (data.is_reactivation && data.existing_username) {
          setUsername(data.existing_username);
        }
      } catch (err) {
        if (cancelled) return;
        setPreviewError(
          err instanceof ApiResponseError && err.status === 410
            ? "This invitation is no longer available."
            : "Could not load invitation. Try the link from your email again.",
        );
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token]);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitError("");
    setSubmitting(true);
    try {
      const data = await apiFetch<{ access_token: string }>(
        "/api/v1/orgs/invitations/accept",
        {
          method: "POST",
          body: JSON.stringify({ token, username, password }),
        },
      );
      setAccessToken(data.access_token);
      await refreshMe();
      router.push("/dashboard");
    } catch (err) {
      setSubmitError(
        err instanceof Error ? err.message : "Could not accept invitation.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner />
      </div>
    );
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center px-4">
      <ThemeToggle className="absolute right-6 top-6" />
      <div className="w-full max-w-sm">
        <div className="mb-8 text-center">
          <h1 className="font-display text-3xl font-semibold text-text-primary">
            The Better Decision
          </h1>
          <p className="mt-1.5 text-sm text-text-muted">Accept invitation</p>
        </div>

        {previewError ? (
          <div className={errorCls} role="alert">
            <p>{previewError}</p>
            <p className="mt-2 text-xs">
              <Link href="/login" className="text-accent hover:text-accent-hover">
                Go to sign in
              </Link>
            </p>
          </div>
        ) : preview ? (
          <form onSubmit={handleSubmit} className="space-y-5">
            <div className="rounded-md border border-border bg-surface-raised px-4 py-3 text-sm">
              <p className="text-text-secondary">
                {preview.is_reactivation
                  ? `Set a new password to rejoin `
                  : `You've been invited to join `}
                <span className="font-medium text-text-primary">
                  {preview.org_name}
                </span>
                {" as "}
                <span className="font-medium text-text-primary">
                  {preview.role}
                </span>
                .
              </p>
              <p className="mt-1 text-xs text-text-muted">
                For <span className="font-medium">{preview.email}</span>
              </p>
            </div>

            {submitError && (
              <div className={errorCls} role="alert">
                {submitError}
              </div>
            )}

            <div>
              <label htmlFor="invite-username" className={label}>
                Username
              </label>
              <input
                id="invite-username"
                type="text"
                required
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                className={input}
                autoComplete="username"
                readOnly={preview.is_reactivation}
              />
            </div>
            <div>
              <label htmlFor="invite-password" className={label}>
                {preview.is_reactivation ? "New password" : "Password"}
              </label>
              <input
                id="invite-password"
                type="password"
                required
                minLength={8}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className={input}
                autoComplete="new-password"
              />
            </div>
            <button
              type="submit"
              disabled={submitting}
              className={`w-full ${btnPrimary}`}
            >
              {submitting
                ? "Joining..."
                : preview.is_reactivation
                  ? "Rejoin organization"
                  : "Accept and create account"}
            </button>
          </form>
        ) : null}
      </div>
    </div>
  );
}
