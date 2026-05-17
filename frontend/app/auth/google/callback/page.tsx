"use client";

import { useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import { setAccessToken } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";

// sessionStorage flag the onboarding wizard reads to know it should
// render the first-run SSO privacy disclosure as Step 0. Only set on
// the new-user branch of the Google callback (the backend appends
// `&created_user=true` to the fragment then). Cleared by the wizard
// after the user accepts the disclosure so the surface never repeats.
//
// Exported as a named constant so the onboarding component and tests
// can reference the exact key — drift between writer + reader would
// silently disable the disclosure for every new SSO user.
export const SSO_DISCLOSURE_PENDING_KEY = "tbd-sso-disclosure-pending";

export default function GoogleCallbackPage() {
  const router = useRouter();
  const { refreshMe } = useAuth();
  const calledRef = useRef(false);

  useEffect(() => {
    if (calledRef.current) return;
    calledRef.current = true;

    // Token is in the URL fragment (#token=xxx) to prevent leaks in
    // server logs, Referer headers, and browser history. The
    // first-run signal `created_user=true` rides on the same
    // fragment so it inherits the same privacy posture.
    const hash = window.location.hash.substring(1); // remove #
    const params = new URLSearchParams(hash);
    const token = params.get("token");
    const createdUser = params.get("created_user") === "true";

    if (!token) {
      router.replace("/login");
      return;
    }

    // Clear the fragment from the URL immediately
    window.history.replaceState(null, "", window.location.pathname);

    setAccessToken(token);

    // Stash the disclosure flag BEFORE refreshMe resolves so the
    // onboarding page sees it on first paint. sessionStorage is
    // tab-scoped and clears on tab close, which is the right blast
    // radius — if the user closes the tab before clicking Continue
    // they will see the disclosure again on the next sign-in, which
    // is acceptable.
    if (createdUser) {
      try {
        window.sessionStorage.setItem(SSO_DISCLOSURE_PENDING_KEY, "1");
      } catch {
        // sessionStorage may be unavailable in private mode. The
        // disclosure simply does not surface in that session — the
        // wizard proceeds normally. Non-fatal.
      }
    }

    refreshMe()
      .then(() => {
        // New SSO users land on /onboarding (the wizard reads the
        // flag and shows the disclosure as Step 0). Returning users
        // go straight to /dashboard; the AuthProvider/onboarding
        // redirect logic upstream handles bookmarked /onboarding
        // visits for already-onboarded users.
        router.replace(createdUser ? "/onboarding" : "/dashboard");
      })
      .catch(() => {
        router.replace("/login");
      });
  }, [router, refreshMe]);

  return (
    <div className="flex min-h-screen items-center justify-center">
      <p className="text-sm text-text-muted">Signing you in...</p>
    </div>
  );
}
