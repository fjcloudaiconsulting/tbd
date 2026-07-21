"use client";

import Link from "next/link";
import { useEffect } from "react";
import { useRouter } from "next/navigation";
import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";
import { hasPlatformPermission } from "@/lib/auth";
import { card, cardTitle, pageTitle } from "@/lib/styles";

type SystemSection = {
  href: string;
  title: string;
  description: string;
  // `permission` is checked via hasPlatformPermission for non-superadmin cards.
  // Omit it on `superadminOnly` cards — the backend already enforces is_superadmin.
  permission?: string;
  // When true the card is only rendered for superadmins, regardless of
  // permission checks. Set this for surfaces where the backend gate is
  // is_superadmin rather than a named platform permission, so plain admins
  // (who pass hasPlatformPermission for broad permissions) don't see a card
  // whose API calls would 403 them.
  superadminOnly?: boolean;
};

// Catalog of /system/* subsections. Add a row here when a new
// platform-admin surface lands. Each card declares the platform
// permission its destination requires, so users only see cards
// whose target page they can open.
const SECTIONS: readonly SystemSection[] = [
  {
    href: "/system/plans",
    title: "Plan Catalog",
    description:
      "Manage subscription plans (free, premium, custom) and per-plan feature flags. Duplicate a plan to build a custom variant for sales-negotiated deals.",
    permission: "plans.manage",
  },
  {
    href: "/system/announcements",
    title: "Announcements",
    description:
      "Publish in-app announcement banners and send email broadcasts to members. Schedule, target by severity, and review delivery status.",
    superadminOnly: true,
  },
  {
    href: "/system/features",
    title: "Feature Flags",
    description:
      "Globally enable or disable gated features (Reports, Plans). Per-org overrides are managed from each organization.",
    superadminOnly: true,
  },
  {
    href: "/system/api-tokens",
    title: "API Tokens",
    description:
      "Mint, list, and revoke long-lived personal access tokens for calling the API from automation and scripts. Reveal-once, scoped, and expiry-capped.",
    superadminOnly: true,
  },
];

export default function SystemHubPage() {
  const { user, loading } = useAuth();
  const router = useRouter();

  // Hub-page guard. There is no dedicated "system.view" permission in
  // the backend catalog, so we reuse admin.view (mirrors PR #171's
  // /admin hub guard). The backend still gates each /system/* call on
  // its specific permission — this client guard just avoids flashing
  // the hub shell to a user who can't open any sub-page.
  const canViewHub = hasPlatformPermission(user, "admin.view");

  // Two-branch guard: AppShell can't redirect from a null render, so we
  // explicitly send unauthenticated visitors to /login and authenticated
  // users without admin.view to /dashboard. Render-null below stays put
  // while either redirect resolves.
  useEffect(() => {
    if (loading) return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (!canViewHub) {
      router.replace("/dashboard");
      return;
    }
  }, [user, loading, canViewHub, router]);

  if (loading || !canViewHub) return null;

  // Hide cards whose destination the current user lacks permission to
  // open. Today /me does not return permissions, so non-superadmins
  // resolve to false on every key — keeps behavior identical to the
  // previous is_superadmin gate. When backend /me starts returning
  // permissions, cards light up automatically per-permission.
  //
  // `superadminOnly` cards bypass the permission check and require
  // is_superadmin directly, because the backend gate on those surfaces is
  // is_superadmin (not a named permission) — plain admins would 403.
  const visibleSections = SECTIONS.filter((section) => {
    if (section.superadminOnly) return user?.is_superadmin === true;
    return hasPlatformPermission(user, section.permission ?? "");
  });

  return (
    <AppShell>
      <h1 className={pageTitle}>System</h1>
      <p className="mt-1 mb-6 text-sm text-text-secondary">
        Platform-admin surface. Tenants and members live under{" "}
        <Link href="/admin" className="text-accent hover:underline">/admin</Link>;
        the surfaces below configure the platform itself.
      </p>

      {visibleSections.length === 0 ? (
        <p className="text-sm text-text-muted">
          You don&apos;t have access to any platform-admin surfaces yet.
        </p>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          {visibleSections.map((section) => (
            <Link key={section.href} href={section.href} className={`${card} block p-5 transition-colors hover:border-accent`}>
              <h2 className={cardTitle}>{section.title}</h2>
              <p className="mt-2 text-sm text-text-secondary">{section.description}</p>
              <p className="mt-3 text-xs font-medium text-accent">Open →</p>
            </Link>
          ))}
        </div>
      )}
    </AppShell>
  );
}
