"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import {
  CONSENT_OPEN_EVENT,
  gtagConsentUpdate,
  readConsent,
  writeConsent,
} from "@/lib/consent";
import { btnPrimary, btnSecondary } from "@/lib/styles";

// EEA/UK cookie-consent banner for the apex marketing site. GA runs in
// Consent Mode v2 with everything denied by default (see GoogleAnalytics.tsx);
// this banner is how the visitor grants/declines, and the only thing that flips
// analytics_storage to granted. Apex-only — mounted behind isApexBuild in the
// layout. See specs/2026-06-20-apex-consent-mode-design.md.
export default function ConsentBanner() {
  // SSR-safe: render nothing until we've checked storage on the client, so the
  // static export never ships a banner in its HTML and there is no hydration
  // mismatch. `open` is decided in the effect below.
  const [open, setOpen] = useState(false);
  const [customizing, setCustomizing] = useState(false);
  const [analytics, setAnalytics] = useState(true);
  const [marketing, setMarketing] = useState(true);

  useEffect(() => {
    // Show the banner when there's no valid (non-expired) stored choice.
    if (!readConsent(Date.now())) setOpen(true);

    // The footer "Cookie preferences" link re-opens the banner regardless of
    // any stored choice, so consent can be withdrawn as easily as it was given.
    function reopen() {
      const existing = readConsent(Date.now());
      if (existing) {
        setAnalytics(existing.analytics);
        setMarketing(existing.marketing);
      }
      setCustomizing(true);
      setOpen(true);
    }
    window.addEventListener(CONSENT_OPEN_EVENT, reopen);
    return () => window.removeEventListener(CONSENT_OPEN_EVENT, reopen);
  }, []);

  function persist(choice: { analytics: boolean; marketing: boolean }) {
    writeConsent(choice, Date.now());
    gtagConsentUpdate(choice);
    setOpen(false);
    setCustomizing(false);
  }

  if (!open) return null;

  return (
    <div
      role="dialog"
      aria-modal="false"
      aria-label="Cookie consent"
      className="fixed inset-x-0 bottom-0 z-50 border-t border-border bg-surface-raised shadow-lg"
    >
      <div className="mx-auto flex max-w-6xl flex-col gap-4 px-6 py-5 lg:px-10">
        <div className="flex flex-col gap-2 text-sm text-text-secondary">
          <p>
            We use cookies to understand how the site is used. Analytics stay off
            until you accept. See our{" "}
            <Link href="/privacy" className="text-accent hover:underline">
              Privacy Policy
            </Link>
            .
          </p>
        </div>

        {customizing && (
          <fieldset className="flex flex-col gap-2 rounded-md border border-border bg-bg p-3">
            <legend className="px-1 text-xs font-medium uppercase tracking-wider text-text-muted">
              Manage preferences
            </legend>
            <label className="flex items-center gap-2 text-sm text-text-muted">
              <input
                type="checkbox"
                checked
                disabled
                aria-label="Necessary cookies (always on)"
              />
              <span>
                <strong className="text-text-secondary">Necessary</strong>:
                always on. Required for the site to function.
              </span>
            </label>
            <label className="flex items-center gap-2 text-sm text-text-secondary">
              <input
                type="checkbox"
                checked={analytics}
                onChange={(e) => setAnalytics(e.target.checked)}
                aria-label="Analytics cookies"
              />
              <span>
                <strong>Analytics</strong>: measure aggregate, anonymized usage.
              </span>
            </label>
            <label className="flex items-center gap-2 text-sm text-text-secondary">
              <input
                type="checkbox"
                checked={marketing}
                onChange={(e) => setMarketing(e.target.checked)}
                aria-label="Marketing cookies"
              />
              <span>
                <strong>Marketing</strong>: measure ad performance (not
                currently active).
              </span>
            </label>
          </fieldset>
        )}

        <div className="flex flex-wrap items-center justify-end gap-2">
          <button
            type="button"
            className={btnSecondary}
            onClick={() => persist({ analytics: false, marketing: false })}
          >
            Reject
          </button>
          {customizing ? (
            <button
              type="button"
              className={btnPrimary}
              onClick={() => persist({ analytics, marketing })}
            >
              Save preferences
            </button>
          ) : (
            <>
              <button
                type="button"
                className={btnSecondary}
                onClick={() => setCustomizing(true)}
              >
                Customize
              </button>
              <button
                type="button"
                className={btnPrimary}
                onClick={() => persist({ analytics: true, marketing: true })}
              >
                Accept
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
