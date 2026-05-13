"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import Link from "next/link";
import {
  ArrowLeftRight,
  BarChart3,
  Building2,
  ChevronUp,
  CreditCard,
  FileText,
  HelpCircle,
  LayoutDashboard,
  LogOut,
  Menu,
  PieChart,
  RefreshCw,
  Settings,
  Shield,
  Tag,
  Wallet,
  X,
} from "lucide-react";
import { useAuth } from "@/components/auth/AuthProvider";
import AppShellAddTransactionCta, {
  shouldShowAddTransactionCta,
} from "@/components/AppShellAddTransactionCta";
import AppShellFooter from "@/components/AppShellFooter";
import { Logo } from "@/components/brand/Logo";
import ThemeToggle from "@/components/ui/ThemeToggle";
import TrialBanner from "@/components/ui/TrialBanner";
import { hasPlatformPermission } from "@/lib/auth";
import { useFocusTrap } from "@/lib/hooks/use-focus-trap";

// Shared sizing/stroke for the sidebar nav icons. Matches the previous
// Heroicons-outline visuals (1.5 stroke, 18×18) so the swap to Lucide is
// purely a maintenance win, not a visual change.
const NAV_ICON_PROPS = {
  "aria-hidden": true as const,
  className: "h-[18px] w-[18px]",
  strokeWidth: 1.5,
} as const;

const navItems = [
  {
    href: "/dashboard",
    label: "Dashboard",
    icon: <LayoutDashboard {...NAV_ICON_PROPS} />,
  },
  {
    href: "/transactions",
    label: "Transactions",
    icon: <ArrowLeftRight {...NAV_ICON_PROPS} />,
  },
  {
    href: "/accounts",
    label: "Accounts",
    icon: <Wallet {...NAV_ICON_PROPS} />,
  },
  {
    href: "/recurring",
    label: "Recurring",
    icon: <RefreshCw {...NAV_ICON_PROPS} />,
  },
  {
    href: "/budgets",
    label: "Budgets",
    icon: <PieChart {...NAV_ICON_PROPS} />,
  },
  {
    href: "/forecast-plans",
    label: "Forecast Plans",
    icon: <BarChart3 {...NAV_ICON_PROPS} />,
  },
  {
    href: "/categories",
    label: "Categories",
    icon: <Tag {...NAV_ICON_PROPS} />,
  },
];

// Per-item permission gating: each System nav link declares the
// platform permission its destination requires. AppShell renders only
// the items whose permission the current user holds. A user with one
// permission (e.g. audit.view) sees just that link; the System section
// header itself appears whenever the filtered list is non-empty.
type SystemNavItem = {
  href: string;
  label: string;
  permission: string;
  icon: React.ReactNode;
};

const systemItems: readonly SystemNavItem[] = [
  {
    href: "/admin",
    label: "Admin",
    permission: "admin.view",
    icon: <Shield {...NAV_ICON_PROPS} />,
  },
  {
    href: "/admin/orgs",
    label: "Organizations",
    permission: "orgs.view",
    icon: <Building2 {...NAV_ICON_PROPS} />,
  },
  {
    href: "/admin/audit",
    label: "Audit log",
    permission: "audit.view",
    icon: <FileText {...NAV_ICON_PROPS} />,
  },
  {
    href: "/admin/analytics",
    label: "Analytics",
    permission: "analytics.view",
    icon: <BarChart3 {...NAV_ICON_PROPS} />,
  },
  {
    href: "/system/plans",
    label: "Plans",
    permission: "plans.manage",
    icon: <CreditCard {...NAV_ICON_PROPS} />,
  },
];

export default function AppShell({ children }: { children: React.ReactNode }) {
  const { user, loading, logout } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const [userExpanded, setUserExpanded] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const sidebarRef = useRef<HTMLElement | null>(null);

  useFocusTrap({ active: sidebarOpen, containerRef: sidebarRef });

  useEffect(() => {
    if (!loading && !user) router.replace("/login");
  }, [user, loading, router]);

  // L3.3 first-run wizard. Bounce authenticated users whose backend
  // explicitly tells us they have not onboarded yet (`onboarded_at`
  // === null). `undefined` means the field is absent from this
  // response shape (test fixtures, forward/backwards compat) — treat
  // those as already-onboarded so the redirect does not hijack
  // unrelated flows.
  useEffect(() => {
    if (loading || !user) return;
    if (user.onboarded_at !== null) return;
    if (pathname === "/onboarding") return;
    if (pathname.startsWith("/accept-invite")) return;
    if (pathname.startsWith("/verify-email")) return;
    router.replace("/onboarding");
  }, [user, loading, pathname, router]);

  useEffect(() => {
    if (!userExpanded) return;
    const close = () => setUserExpanded(false);
    document.addEventListener("click", close);
    return () => document.removeEventListener("click", close);
  }, [userExpanded]);

  // Escape closes the mobile drawer. useFocusTrap doesn't handle this
  // itself; it only manages Tab cycling and focus restore.
  useEffect(() => {
    if (!sidebarOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setSidebarOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [sidebarOpen]);

  if (loading || !user) {
    return (
      <div className="flex h-screen items-center justify-center bg-bg" role="status" aria-label="Loading">
        <div className="h-6 w-6 animate-spin rounded-full border-2 border-border border-t-accent" />
      </div>
    );
  }

  // Filter System nav items per-link. A user with `audit.view` but no
  // `orgs.view` should see Audit log without seeing Organizations — and
  // a user with no platform permissions should not see the System
  // section at all (visibleSystemItems is empty, header hidden).
  const visibleSystemItems = systemItems.filter((item) =>
    hasPlatformPermission(user, item.permission),
  );
  const showSystemSection = visibleSystemItems.length > 0;

  // All hrefs that could potentially match the current pathname.
  // Used to break ties: when both `/admin` and `/admin/orgs` would
  // match the path `/admin/orgs` under a naive prefix check, only
  // the longest match wins so the parent doesn't double-highlight.
  const allHrefs = [...navItems, ...systemItems].map((i) => i.href);
  function isActive(href: string) {
    if (pathname === href) return true;
    if (!pathname.startsWith(href + "/")) return false;
    const longerMatch = allHrefs.some(
      (other) =>
        other !== href &&
        other.length > href.length &&
        (pathname === other || pathname.startsWith(other + "/")),
    );
    return !longerMatch;
  }

  return (
    <div className="flex h-screen overflow-hidden">
      {/* Mobile overlay backdrop — real <button> so keyboard users can dismiss */}
      {sidebarOpen && (
        <button
          type="button"
          aria-label="Close menu"
          onClick={() => setSidebarOpen(false)}
          className="fixed inset-0 z-40 bg-bg/80 lg:hidden"
        />
      )}

      {/* Dark sidebar — fixed height, never scrolls */}
      <aside ref={sidebarRef} className={`fixed inset-y-0 left-0 z-50 flex w-56 flex-col bg-sidebar-bg transition-transform duration-200 lg:relative lg:translate-x-0 ${sidebarOpen ? "translate-x-0" : "-translate-x-full lg:translate-x-0"}`}>
        <div className="flex items-center justify-between px-5 pt-5 pb-6">
          <Link
            href="/dashboard"
            aria-label="The Better Decision — Dashboard"
            className="inline-flex items-center text-sidebar-text-bright"
          >
            {/* Sidebar ground is dark; the muted Logo tone keeps the
                lockup at slate-on-slate weight so it doesn't fight the
                primary navigation for emphasis. */}
            <Logo tone="muted" size="sm" short />
          </Link>
          <button onClick={() => setSidebarOpen(false)} aria-label="Close menu" className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded-md text-sidebar-muted hover:text-sidebar-text-bright lg:hidden">
            <X aria-hidden="true" className="h-5 w-5" strokeWidth={2} />
          </button>
        </div>

        <nav className="flex-1 overflow-y-auto space-y-0.5 px-3">
          {navItems.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              onClick={() => setSidebarOpen(false)}
              className={`flex items-center gap-3 rounded-lg px-3 py-2.5 text-[13px] font-medium transition-colors ${
                isActive(item.href)
                  ? "bg-sidebar-active-bg text-sidebar-active-text"
                  : "text-sidebar-text hover:bg-sidebar-hover hover:text-sidebar-text-bright"
              }`}
            >
              {item.icon}
              {item.label}
            </Link>
          ))}

          {showSystemSection && (
            <>
              <div className="pb-1 pt-6 px-3">
                <span className="text-[10px] font-semibold uppercase tracking-[0.12em] text-sidebar-muted">
                  System
                </span>
              </div>
              {visibleSystemItems.map((item) => (
                <Link
                  key={item.href}
                  href={item.href}
                  onClick={() => setSidebarOpen(false)}
                  className={`flex items-center gap-3 rounded-lg px-3 py-2.5 text-[13px] font-medium transition-colors ${
                    isActive(item.href)
                      ? "bg-sidebar-active-bg text-sidebar-active-text"
                      : "text-sidebar-text hover:bg-sidebar-hover hover:text-sidebar-text-bright"
                  }`}
                >
                  {item.icon}
                  {item.label}
                </Link>
              ))}
            </>
          )}
        </nav>

        {/* User section at bottom */}
        <div className="relative border-t border-sidebar-border px-3 py-3">
          <button
            onClick={(e) => {
              e.stopPropagation();
              setUserExpanded(!userExpanded);
            }}
            className="flex w-full items-center gap-3 rounded-lg px-3 py-2.5 text-left transition-colors hover:bg-sidebar-hover"
          >
            <div className="flex h-7 w-7 items-center justify-center rounded-full bg-sidebar-active-bg text-xs font-semibold text-sidebar-active-text">
              {(user.first_name || user.username).charAt(0).toUpperCase()}
            </div>
            <div className="flex-1 min-w-0">
              <p className="truncate text-[13px] font-medium text-sidebar-text-bright">{user.first_name || user.username}</p>
              <p className="truncate text-[11px] text-sidebar-muted">{user.org_name}</p>
            </div>
            <ChevronUp
              aria-hidden="true"
              className={`h-3.5 w-3.5 text-sidebar-muted transition-transform ${userExpanded ? "rotate-180" : ""}`}
              strokeWidth={2}
            />
          </button>

          {userExpanded && (
            <div className="absolute bottom-full left-3 right-3 mb-1.5 rounded-lg border border-sidebar-border bg-sidebar-bg py-1 shadow-xl">
              <Link
                href="/settings"
                onClick={() => setSidebarOpen(false)}
                className="flex items-center gap-2.5 px-3.5 py-2 text-[13px] text-sidebar-text hover:bg-sidebar-hover hover:text-sidebar-text-bright"
              >
                <Settings aria-hidden="true" className="h-4 w-4" strokeWidth={1.5} />
                Settings
              </Link>
              <div className="my-1 border-t border-sidebar-border" />
              <button
                onClick={logout}
                className="flex w-full items-center gap-2.5 px-3.5 py-2 text-[13px] text-sidebar-text hover:bg-sidebar-hover hover:text-sidebar-text-bright"
              >
                <LogOut aria-hidden="true" className="h-4 w-4" strokeWidth={1.5} />
                Sign Out
              </button>
            </div>
          )}
        </div>
      </aside>

      <div className="flex flex-1 flex-col overflow-hidden">
        {/* Header balances by right-aligning the action row on lg+ where
            the menu button is hidden and the sidebar already carries the
            brand. On mobile we keep `justify-between` so the menu button
            anchors left and actions anchor right (addresses the
            "AppShell Header Balance" backlog item). */}
        <header className="flex h-14 shrink-0 items-center justify-between border-b border-border bg-surface px-4 sm:px-8 lg:justify-end">
          <button onClick={() => setSidebarOpen(true)} className="rounded-md p-2 text-text-muted hover:text-text-primary lg:hidden" aria-label="Open menu">
            <Menu aria-hidden="true" className="h-5 w-5" strokeWidth={2} />
          </button>
          <div className="flex items-center gap-3">
            <TrialBanner user={user} />
            {shouldShowAddTransactionCta(pathname) && <AppShellAddTransactionCta />}
            <Link
              href="/docs"
              className="rounded-md p-2 text-text-muted transition-colors hover:text-text-primary"
              aria-label="Docs"
              title="Docs"
            >
              <HelpCircle aria-hidden="true" className="h-5 w-5" strokeWidth={1.5} />
            </Link>
            <ThemeToggle />
          </div>
        </header>
        <main className="flex-1 overflow-auto p-4 sm:p-8"><div className="mx-auto max-w-screen-xl">{children}</div></main>
        <AppShellFooter />
      </div>
    </div>
  );
}
