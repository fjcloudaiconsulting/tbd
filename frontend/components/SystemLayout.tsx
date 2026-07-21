"use client";

import AppShell from "@/components/AppShell";
import SystemBreadcrumb from "@/components/system/SystemBreadcrumb";

// Thin wrapper for a /system/* sub-page: the app chrome plus the shared
// breadcrumb back to the hub. It deliberately does NOT render the page
// title or run an auth guard — each sub-page keeps its own heading (some
// pair the title with an action button) and its own is_superadmin / permission
// guard, returning null before this renders, exactly as they did around a bare
// AppShell. Mirrors SettingsLayout's role for /settings/*.
export default function SystemLayout({
  current,
  children,
}: {
  current: string;
  children: React.ReactNode;
}) {
  return (
    <AppShell>
      <SystemBreadcrumb current={current} />
      {children}
    </AppShell>
  );
}
