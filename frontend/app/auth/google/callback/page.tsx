"use client";

import { Suspense, useEffect, useRef } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import { setAccessToken } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";

function GoogleCallbackHandler() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const { refreshMe } = useAuth();
  const token = searchParams.get("token");
  const calledRef = useRef(false);

  useEffect(() => {
    if (calledRef.current) return;
    calledRef.current = true;

    if (!token) {
      router.replace("/login");
      return;
    }

    setAccessToken(token);
    refreshMe().then(() => {
      router.replace("/dashboard");
    }).catch(() => {
      router.replace("/login");
    });
  }, [token, router, refreshMe]);

  return (
    <div className="flex min-h-screen items-center justify-center">
      <p className="text-sm text-text-muted">Signing you in...</p>
    </div>
  );
}

export default function GoogleCallbackPage() {
  return (
    <Suspense
      fallback={
        <div className="flex min-h-screen items-center justify-center">
          <p className="text-sm text-text-muted">Loading...</p>
        </div>
      }
    >
      <GoogleCallbackHandler />
    </Suspense>
  );
}
