"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

/**
 * Back-navigation link for public pages (privacy, terms, help).
 *
 * Uses the browser's session history so "Back" actually returns to wherever
 * the user came from — landing, login, an external referrer, etc. When the
 * page was opened directly (shared URL in a fresh tab, bookmark, etc.),
 * `window.history.length` is 1 and we fall back to a "Home" link pointing
 * at the landing so the user always has a way out.
 */
export default function BackLink({ className = "" }: { className?: string }) {
  const router = useRouter();
  const [canGoBack, setCanGoBack] = useState(false);

  useEffect(() => {
    // History length > 1 means there's a previous entry for this tab.
    // This check runs client-side only (useEffect), so there's no SSR
    // mismatch — we render the "Home" fallback on first paint and upgrade
    // to "Back" once we know the history state.
    setCanGoBack(window.history.length > 1);
  }, []);

  const baseClasses =
    "text-sm text-text-muted transition-colors hover:text-text-primary";
  const mergedClasses = className ? `${baseClasses} ${className}` : baseClasses;

  if (canGoBack) {
    return (
      <button
        type="button"
        onClick={() => router.back()}
        className={mergedClasses}
      >
        ← Back
      </button>
    );
  }

  return (
    <Link href="/" className={mergedClasses}>
      ← Home
    </Link>
  );
}
