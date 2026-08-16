"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";
import ThemeToggle from "@/components/ui/ThemeToggle";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";
import { error as errorCls, success } from "@/lib/styles";

function VerifyEmailHandler() {
  const searchParams = useSearchParams();
  const token = searchParams.get("token");
  const { user } = useAuth();

  const [status, setStatus] = useState<"loading" | "success" | "error">("loading");
  // TBD-361. A PROMOTION (a confirmed email change) invalidates every
  // session, so the cached `user` here is stale and "Go to dashboard" would
  // walk straight into a 401. A first-time verification changes no identity
  // and leaves the session alone. The backend distinguishes them.
  const [emailChanged, setEmailChanged] = useState(false);
  const [errorMsg, setErrorMsg] = useState("");
  const calledRef = useRef(false);

  useEffect(() => {
    if (!token || calledRef.current) return;
    calledRef.current = true;

    apiFetch("/api/v1/auth/verify-email", {
      method: "POST",
      body: JSON.stringify({ token }),
    })
      .then((res) => {
        setEmailChanged(Boolean((res as { email_changed?: boolean })?.email_changed));
        setStatus("success");
      })
      .catch((err) => {
        setStatus("error");
        setErrorMsg(err instanceof Error ? err.message : "Verification failed");
      });
  }, [token]);

  if (!token) {
    return (
      <div className="space-y-5">
        <div className={errorCls}>Invalid verification link.</div>
        <p className="text-center text-sm text-text-muted">
          <Link href="/login" className="text-accent hover:text-accent-hover">
            Go to login
          </Link>
        </p>
      </div>
    );
  }

  if (status === "loading") {
    return <p className="text-center text-sm text-text-muted">Verifying your email...</p>;
  }

  if (status === "error") {
    return (
      <div className="space-y-5">
        <div className={errorCls}>
          {errorMsg || "Invalid or expired verification link."}
        </div>
        <p className="text-center text-sm text-text-muted">
          <Link href="/login" className="text-accent hover:text-accent-hover">
            Go to login
          </Link>
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <div className={success}>
        {emailChanged ? "Email address confirmed!" : "Email verified!"}
      </div>
      {emailChanged && (
        <p className="text-center text-sm text-text-muted">
          This address is now your sign-in email. For security, confirming a
          new address signs you out everywhere, so please sign in again.
        </p>
      )}
      <p className="text-center text-sm text-text-muted">
        <Link
          href={!emailChanged && user ? "/dashboard" : "/login"}
          className="text-accent hover:text-accent-hover"
        >
          {!emailChanged && user ? "Go to dashboard" : "Sign in"}
        </Link>
      </p>
    </div>
  );
}

export default function VerifyEmailPage() {
  return (
    <div className="relative flex min-h-screen items-center justify-center px-4">
      <ThemeToggle className="absolute right-6 top-6" />

      <div className="w-full max-w-sm">
        <div className="mb-10 text-center">
          <h1 className="font-display text-3xl font-semibold text-text-primary">Email Verification</h1>
        </div>
        <Suspense fallback={<p className="text-center text-sm text-text-muted">Loading...</p>}>
          <VerifyEmailHandler />
        </Suspense>
      </div>
    </div>
  );
}
