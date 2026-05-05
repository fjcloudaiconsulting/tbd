import Link from "next/link";
import { btnPrimary, btnSecondary, card, cardTitle } from "@/lib/styles";

// Custom 404. Server component, auth-neutral. Cannot read auth state
// or use hooks — those would require "use client" and break the
// statically-rendered fallback contract that lets / and /login also
// resolve through this page when their slugs don't match.
export const metadata = {
  title: "Page not found",
  robots: { index: false, follow: false },
};

export default function NotFound() {
  return (
    <main className="flex min-h-screen items-center justify-center bg-bg p-4">
      <div className={`${card} max-w-md w-full p-6 text-center`}>
        <p className="font-display text-6xl text-text-primary">404</p>
        <h1 className={`${cardTitle} mt-2`}>Page not found</h1>
        <p className="mt-3 text-sm text-text-secondary">
          The page you&rsquo;re looking for doesn&rsquo;t exist or was moved.
        </p>
        <div className="mt-5 flex flex-col gap-2 sm:flex-row sm:justify-center">
          <Link
            href="/dashboard"
            className={`${btnPrimary} w-full sm:w-auto min-h-[44px] sm:min-h-0 inline-flex items-center justify-center`}
          >
            Go to dashboard
          </Link>
          <Link
            href="/"
            className={`${btnSecondary} w-full sm:w-auto min-h-[44px] sm:min-h-0 inline-flex items-center justify-center`}
          >
            Visit landing page
          </Link>
        </div>
      </div>
    </main>
  );
}
