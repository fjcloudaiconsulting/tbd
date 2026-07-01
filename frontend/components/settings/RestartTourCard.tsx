"use client";

/**
 * RestartTourCard — surfaces the L3.3 "Replay the dashboard tour" action.
 *
 * Per-user (not org-wide), so it lives on the Profile tab rather than
 * Organization. Calls ``POST /api/v1/users/me/onboarding/restart-tour``
 * as an audit-only server action (the backend deliberately does NOT
 * mutate ``users.onboarded_at`` — AppShell guards on that column to
 * bounce first-run users to ``/onboarding``, so clearing it would
 * trap the user in a redirect loop). After the audit call we set the
 * same sessionStorage flag the wizard's "Yes, show me" path uses so
 * the dashboard auto-starts the dot-namespaced tour on next mount.
 *
 * The endpoint is idempotent — a second click before the dashboard
 * mounts will not error, only emit another audit row.
 */
import { useState } from "react";
import { useRouter } from "next/navigation";

import { useAuth } from "@/components/auth/AuthProvider";
import { useTour } from "@/components/tour/useTour";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { btnSecondary, card, cardHeader, cardTitle } from "@/lib/styles";

import {
  DASHBOARD_TOUR_STEPS,
  TOUR_FLAG_KEY,
  TOUR_FLAG_VALUE_DASHBOARD,
} from "@/lib/help/tour";
import { safeTourStorageSet } from "@/lib/help/tourStorage";

export default function RestartTourCard() {
  const router = useRouter();
  const { refreshMe } = useAuth();
  const tour = useTour();
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleRestart() {
    setSubmitting(true);
    setError(null);
    try {
      await apiFetch("/api/v1/users/me/onboarding/restart-tour", {
        method: "POST",
      });
      // Refresh the cached user so any other AppShell guards see the
      // freshest /me payload, then stage the dashboard auto-start.
      // Note: the backend intentionally leaves ``onboarded_at`` untouched,
      // so AppShell will NOT redirect us to /onboarding after this call.
      await refreshMe();
      // Primary path: stage the start on the TourContext, which sits
      // above the page tree and survives the navigation to /dashboard
      // (and works in Safari private mode where sessionStorage throws).
      tour.requestStart(DASHBOARD_TOUR_STEPS);
      // Secondary fallback: the stored flag still covers a full page
      // reload between this click and the dashboard mount. safeTourStorageSet
      // writes sessionStorage when available and an in-memory fallback
      // otherwise (private mode / storage disabled), so it never throws.
      safeTourStorageSet(TOUR_FLAG_KEY, TOUR_FLAG_VALUE_DASHBOARD);
      router.push("/dashboard");
    } catch (err) {
      setError(extractErrorMessage(err, "Could not restart the tour. Try again."));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className={card}>
      <div className={cardHeader}>
        <h2 className={cardTitle}>Dashboard tour</h2>
      </div>
      <div className="p-6 space-y-4">
        <p className="text-sm text-text-secondary">
          Replay the dashboard tour to refresh your memory or to show
          a colleague how The Better Decision works. Replaying does not
          touch any of your data.
        </p>
        {error ? (
          <p role="alert" className="text-sm text-danger">
            {error}
          </p>
        ) : null}
        <button
          type="button"
          onClick={handleRestart}
          disabled={submitting}
          className={btnSecondary}
          data-testid="settings-restart-tour"
        >
          {submitting ? "Starting..." : "Replay the dashboard tour"}
        </button>
      </div>
    </div>
  );
}
