"use client";

import { useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import { setAccessToken } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";

export default function GoogleCallbackPage() {
  const router = useRouter();
  const { refreshMe } = useAuth();
  const calledRef = useRef(false);

  useEffect(() => {
    if (calledRef.current) return;
    calledRef.current = true;

    // Token is in the URL fragment (#token=xxx) to prevent leaks in
    // server logs, Referer headers, and browser history
    const hash = window.location.hash.substring(1); // remove #
    const params = new URLSearchParams(hash);
    const token = params.get("token");

    if (!token) {
      router.replace("/login");
      return;
    }

    // Clear the fragment from the URL immediately
    window.history.replaceState(null, "", window.location.pathname);

    setAccessToken(token);
    refreshMe().then(() => {
      router.replace("/dashboard");
    }).catch(() => {
      router.replace("/login");
    });
  }, [router, refreshMe]);

  return (
    <div className="flex min-h-screen items-center justify-center">
      <p className="text-sm text-text-muted">Signing you in...</p>
    </div>
  );
}
