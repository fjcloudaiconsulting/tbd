"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/components/auth/AuthProvider";

export default function RootPage() {
  const { user, loading, needsSetup } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!loading) {
      if (needsSetup) {
        router.replace("/setup");
      } else if (user) {
        router.replace("/dashboard");
      } else {
        router.replace("/login");
      }
    }
  }, [user, loading, needsSetup, router]);

  return (
    <div className="flex h-screen items-center justify-center bg-bg">
      <div className="h-6 w-6 animate-spin rounded-full border-2 border-border border-t-accent" />
    </div>
  );
}
