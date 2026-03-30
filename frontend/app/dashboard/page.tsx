"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/components/auth/AuthProvider";

export default function DashboardPage() {
  const { user, loading, logout } = useAuth();
  const router = useRouter();

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

  return (
    <div className="min-h-screen">
      <nav className="border-b border-gray-200 bg-white">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-4 py-3">
          <h1 className="text-lg font-bold">PFV2</h1>
          <div className="flex items-center gap-4">
            <span className="text-sm text-gray-600">
              {user.username}{" "}
              <span className="text-gray-400">({user.org_name})</span>
            </span>
            <button
              onClick={logout}
              className="rounded px-3 py-1 text-sm text-gray-600 hover:bg-gray-100"
            >
              Sign Out
            </button>
          </div>
        </div>
      </nav>
      <main className="mx-auto max-w-7xl px-4 py-8">
        <h2 className="mb-4 text-xl font-semibold">Dashboard</h2>
        <p className="text-gray-600">
          Welcome, {user.username}. Your finance dashboard will appear here.
        </p>
      </main>
    </div>
  );
}
