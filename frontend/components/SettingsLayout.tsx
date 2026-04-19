"use client";

import Link from "next/link";
import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";
import { isAdmin, isOwner } from "@/lib/auth";
import { pageTitle } from "@/lib/styles";

const tabs = [
  { href: "/settings", label: "Profile", minRole: "member" as const },
  { href: "/settings/security", label: "Security", minRole: "member" as const },
  { href: "/settings/organization", label: "Organization", minRole: "admin" as const },
  { href: "/settings/billing", label: "Billing", minRole: "owner" as const },
];

export default function SettingsLayout({ children, activeTab }: { children: React.ReactNode; activeTab: string }) {
  const { user, loading } = useAuth();

  if (loading || !user) {
    return (
      <AppShell>
        <h1 className={pageTitle}>Settings</h1>
        <div className="flex justify-center py-12">
          <div className="h-6 w-6 animate-spin rounded-full border-2 border-border border-t-accent" />
        </div>
      </AppShell>
    );
  }

  const visibleTabs = tabs.filter((tab) => {
    if (tab.minRole === "owner") return isOwner(user);
    if (tab.minRole === "admin") return isAdmin(user);
    return true;
  });

  return (
    <AppShell>
      <h1 className={pageTitle}>Settings</h1>
      <nav className="mb-6 flex gap-0 overflow-x-auto border-b border-border -mx-4 px-4 sm:mx-0 sm:px-0">
        {visibleTabs.map((tab) => (
          <Link
            key={tab.href}
            href={tab.href}
            className={`whitespace-nowrap px-5 py-3 text-sm font-medium transition-colors ${
              activeTab === tab.href
                ? "border-b-2 border-accent text-accent"
                : "text-text-muted hover:text-text-primary"
            }`}
          >
            {tab.label}
          </Link>
        ))}
      </nav>
      {children}
    </AppShell>
  );
}
