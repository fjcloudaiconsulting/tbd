"use client";

import Link from "next/link";
import { useEffect } from "react";
import { useRouter } from "next/navigation";
import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";
import { card, cardTitle, pageTitle } from "@/lib/styles";

type SystemSection = {
  href: string;
  title: string;
  description: string;
};

// Catalog of /system/* subsections. Add a row here when a new
// platform-admin surface lands — keeps the hub honest without
// touching the gate logic below.
const SECTIONS: SystemSection[] = [
  {
    href: "/system/plans",
    title: "Plans",
    description:
      "Manage subscription plans (free, premium, custom) and per-plan feature flags. Duplicate a plan to build a custom variant for sales-negotiated deals.",
  },
];

export default function SystemHubPage() {
  const { user, loading } = useAuth();
  const router = useRouter();

  // Client-side guard: redirect non-superadmins. The backend gates on
  // platform permissions for every /system/* call — this just keeps a
  // regular user from seeing a half-rendered hub before the redirect.
  useEffect(() => {
    if (!loading && (!user || !user.is_superadmin)) {
      router.replace("/dashboard");
    }
  }, [user, loading, router]);

  if (loading || !user?.is_superadmin) return null;

  return (
    <AppShell>
      <h1 className={pageTitle}>System</h1>
      <p className="mt-1 mb-6 text-sm text-text-secondary">
        Platform-admin surface. Tenants and members live under{" "}
        <Link href="/admin" className="text-accent hover:underline">/admin</Link>;
        the surfaces below configure the platform itself.
      </p>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        {SECTIONS.map((section) => (
          <Link key={section.href} href={section.href} className={`${card} block p-5 transition-colors hover:border-accent`}>
            <h2 className={cardTitle}>{section.title}</h2>
            <p className="mt-2 text-sm text-text-secondary">{section.description}</p>
            <p className="mt-3 text-xs font-medium text-accent">Open →</p>
          </Link>
        ))}
      </div>
    </AppShell>
  );
}
