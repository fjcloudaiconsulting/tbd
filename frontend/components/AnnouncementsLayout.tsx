"use client";

import Link from "next/link";
import { useEffect } from "react";
import { useRouter } from "next/navigation";

import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";
import { isSuperadmin } from "@/lib/auth";
import { pageTitle } from "@/lib/styles";

const tabs = [
  { href: "/system/announcements", label: "In-app" },
  { href: "/system/announcements/broadcasts", label: "Email broadcasts" },
];

export default function AnnouncementsLayout({
  children,
  activeTab,
}: {
  children: React.ReactNode;
  activeTab: string;
}) {
  const { user, loading } = useAuth();
  const router = useRouter();

  const canManage = !!user && isSuperadmin(user);

  useEffect(() => {
    if (loading) return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (!canManage) {
      router.replace("/dashboard");
      return;
    }
  }, [loading, user, canManage, router]);

  if (loading) {
    return (
      <AppShell>
        <h1 className={pageTitle}>Announcements</h1>
        <div className="flex justify-center py-12">
          <div className="h-6 w-6 animate-spin rounded-full border-2 border-border border-t-accent" />
        </div>
      </AppShell>
    );
  }

  if (!canManage) return null;

  return (
    <AppShell>
      <h1 className={pageTitle}>Announcements</h1>
      <nav className="mb-6 flex gap-0 overflow-x-auto border-b border-border -mx-4 px-4 sm:mx-0 sm:px-0">
        {tabs.map((tab) => (
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
