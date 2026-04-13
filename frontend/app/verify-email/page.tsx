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
  const [errorMsg, setErrorMsg] = useState("");
  const calledRef = useRef(false);

  useEffect(() => {
    if (!token || calledRef.current) return;
    calledRef.current = true;

    apiFetch("/api/v1/auth/verify-email", {
      method: "POST",
      body: JSON.stringify({ token }),
    })
      .then(() => setStatus("success"))
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
      <div className={success}>Email verified!</div>
      <p className="text-center text-sm text-text-muted">
        <Link
          href={user ? "/dashboard" : "/login"}
          className="text-accent hover:text-accent-hover"
        >
          {user ? "Go to dashboard" : "Sign in"}
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
