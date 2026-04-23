"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/components/auth/AuthProvider";

// Client island that runs after hydration to redirect authenticated
// visitors away from the public landing. Deliberately renders null so
// the landing content itself is server-rendered and reaches crawlers
// and no-JS visitors directly.
export default function LandingAuthRedirect() {
  const { user, loading, needsSetup } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (loading) return;
    if (needsSetup) {
      router.replace("/setup");
    } else if (user) {
      router.replace("/dashboard");
    }
  }, [user, loading, needsSetup, router]);

  return null;
}
