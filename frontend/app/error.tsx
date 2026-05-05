"use client";

import { useEffect } from "react";
import Link from "next/link";
import { btnPrimary, btnSecondary, card, cardTitle } from "@/lib/styles";

// Root error boundary. Auth-neutral by design: must not import or
// render AppShell, useAuth, or anything that assumes a session — if
// auth itself crashes, this is the page that has to keep working.
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  // Surface to the browser console in dev so the underlying stack is
  // visible alongside the friendly UI; production noise gets filtered
  // by the structured logger upstream.
  useEffect(() => {
    if (process.env.NODE_ENV !== "production") {
      // eslint-disable-next-line no-console
      console.error("[error.tsx] caught:", error);
    }
  }, [error]);

  return (
    <main className="flex min-h-screen items-center justify-center bg-bg p-4">
      <div className={`${card} max-w-md w-full p-6`}>
        <h1 className={`${cardTitle} text-danger`}>Something went wrong</h1>
        <p className="mt-3 text-sm text-text-secondary">
          The page couldn&rsquo;t be displayed. You can try again, or head back to safer ground.
        </p>
        {error?.digest && (
          <p className="mt-3 text-xs font-mono text-text-muted">
            Reference: <code>{error.digest}</code>
          </p>
        )}
        <div className="mt-5 flex flex-col gap-2 sm:flex-row">
          <button
            type="button"
            onClick={() => reset()}
            className={`${btnPrimary} w-full sm:w-auto min-h-[44px] sm:min-h-0`}
          >
            Try again
          </button>
          <Link
            href="/dashboard"
            className={`${btnSecondary} w-full sm:w-auto min-h-[44px] sm:min-h-0 inline-flex items-center justify-center`}
          >
            Back to dashboard
          </Link>
        </div>
      </div>
    </main>
  );
}
