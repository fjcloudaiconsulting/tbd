"use client";

import { useEffect } from "react";
import { useRouter, usePathname } from "next/navigation";
import Link from "next/link";
import { useAuth } from "@/components/auth/AuthProvider";

const navItems = [
  { href: "/dashboard", label: "Dashboard" },
  { href: "/accounts", label: "Accounts" },
];

const adminItems = [
  { href: "/admin/settings", label: "Settings" },
];

export default function AppShell({ children }: { children: React.ReactNode }) {
  const { user, loading, logout } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (!loading && !user) {
      router.replace("/login");
    }
  }, [user, loading, router]);

  if (loading || !user) {
    return (
      <div className="flex h-screen items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600" />
      </div>
    );
  }

  const isAdmin = user.role === "owner" || user.role === "admin" || user.is_superadmin;

  return (
    <div className="flex min-h-screen">
      {/* Sidebar */}
      <aside className="flex w-56 flex-col border-r border-gray-200 bg-white">
        <div className="border-b border-gray-200 px-4 py-4">
          <Link href="/dashboard" className="text-lg font-bold text-gray-900">
            PFV2
          </Link>
          <p className="mt-0.5 text-xs text-gray-400 truncate">{user.org_name}</p>
        </div>

        <nav className="flex-1 px-2 py-3 space-y-0.5">
          {navItems.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className={`block rounded px-3 py-2 text-sm ${
                pathname === item.href || pathname.startsWith(item.href + "/")
                  ? "bg-blue-50 font-medium text-blue-700"
                  : "text-gray-700 hover:bg-gray-50"
              }`}
            >
              {item.label}
            </Link>
          ))}

          {isAdmin && (
            <>
              <div className="pt-4 pb-1 px-3">
                <span className="text-xs font-medium uppercase text-gray-400">
                  Admin
                </span>
              </div>
              {adminItems.map((item) => (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`block rounded px-3 py-2 text-sm ${
                    pathname === item.href || pathname.startsWith(item.href + "/")
                      ? "bg-blue-50 font-medium text-blue-700"
                      : "text-gray-700 hover:bg-gray-50"
                  }`}
                >
                  {item.label}
                </Link>
              ))}
            </>
          )}
        </nav>

        <div className="border-t border-gray-200 px-2 py-3 space-y-0.5">
          <Link
            href="/profile"
            className={`block rounded px-3 py-2 text-sm ${
              pathname === "/profile"
                ? "bg-blue-50 font-medium text-blue-700"
                : "text-gray-700 hover:bg-gray-50"
            }`}
          >
            Profile
          </Link>
          <button
            onClick={logout}
            className="block w-full rounded px-3 py-2 text-left text-sm text-gray-700 hover:bg-gray-50"
          >
            Sign Out
          </button>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 bg-gray-50 p-6">{children}</main>
    </div>
  );
}
