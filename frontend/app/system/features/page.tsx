"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import AppShell from "@/components/AppShell";
import FeatureFlagsCard from "@/components/system/FeatureFlagsCard";
import { useAuth } from "@/components/auth/AuthProvider";
import { pageTitle } from "@/lib/styles";

export default function SystemFeaturesPage() {
  const { user, loading } = useAuth();
  const router = useRouter();

  // Superadmin-only page. The backend gates every /admin/features call on
  // is_superadmin, so we mirror that guard client-side to avoid flashing the
  // shell to a plain admin whose API calls would 403.
  useEffect(() => {
    if (loading) return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (!user.is_superadmin) {
      router.replace("/dashboard");
      return;
    }
  }, [user, loading, router]);

  if (loading || !user?.is_superadmin) return null;

  return (
    <AppShell>
      <h1 className={pageTitle}>Feature Flags</h1>
      <p className="mt-1 mb-6 text-sm text-text-secondary">
        Set the global default for each gated feature. Per-org overrides are
        managed from each organization&apos;s admin page.
      </p>

      <FeatureFlagsCard />
    </AppShell>
  );
}
