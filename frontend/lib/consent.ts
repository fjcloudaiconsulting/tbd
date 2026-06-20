// Cookie-consent storage + Google Consent Mode v2 mapping for the apex
// marketing site. Pure and framework-free so it is the single source of truth
// shared by the inline Consent Mode bootstrap (GoogleAnalytics.tsx) and the
// React banner (ConsentBanner.tsx) — the two must never diverge.
//
// The apex is a static export with no request-time runtime, so the user's
// choice lives in localStorage rather than a server session. GA only runs on
// the apex (see lib/analytics.ts isApexBuild), so this surface is apex-only.
//
// The localStorage plumbing (SSR guard + JSON guard rails) is reused from
// lib/persisted-state.ts; this module layers only the consent-specific shape
// validation and the 6-month expiry on top.

import { readPersisted, writePersisted } from "@/lib/persisted-state";

export const CONSENT_STORAGE_KEY = "tbd-consent-v1";

// Re-ask after ~6 months — conservative end of CNIL/ICO guidance (6–13 months).
export const CONSENT_TTL_MS = 1000 * 60 * 60 * 24 * 182;

export type ConsentChoice = {
  analytics: boolean;
  marketing: boolean;
  /** Epoch ms the choice was recorded; drives the 6-month re-ask. */
  ts: number;
};

export type ConsentSignal = "granted" | "denied";

// Consent Mode v2 default applied before gtag('config'). Everything
// non-essential is denied until the user opts in; security + functionality are
// granted (they are strictly necessary and need no consent).
//
// personalization_storage is intentionally left denied with no path to grant
// it: the banner exposes no "personalization" category (we use none), so it
// stays denied for the session's lifetime. If a personalization feature is
// ever added, give it a category and extend toConsentModeUpdate.
export const DEFAULT_DENIED: Record<string, ConsentSignal> = {
  ad_storage: "denied",
  ad_user_data: "denied",
  ad_personalization: "denied",
  analytics_storage: "denied",
  personalization_storage: "denied",
  functionality_storage: "granted",
  security_storage: "granted",
};

/**
 * Map a stored choice to the Consent Mode `update` payload.
 *   analytics -> analytics_storage
 *   marketing -> ad_storage + ad_user_data + ad_personalization
 */
export function toConsentModeUpdate(
  choice: Pick<ConsentChoice, "analytics" | "marketing">,
): Record<string, ConsentSignal> {
  const analytics: ConsentSignal = choice.analytics ? "granted" : "denied";
  const marketing: ConsentSignal = choice.marketing ? "granted" : "denied";
  return {
    analytics_storage: analytics,
    ad_storage: marketing,
    ad_user_data: marketing,
    ad_personalization: marketing,
  };
}

function isConsentChoice(value: unknown): value is ConsentChoice {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as ConsentChoice).analytics === "boolean" &&
    typeof (value as ConsentChoice).marketing === "boolean" &&
    typeof (value as ConsentChoice).ts === "number"
  );
}

/** Read + validate the stored choice; null if missing, malformed, or expired. */
export function readConsent(now: number): ConsentChoice | null {
  // readPersisted handles the SSR guard, missing key, malformed JSON, and the
  // shape validation, falling through to null on any of them.
  const choice = readPersisted<ConsentChoice | null>(
    CONSENT_STORAGE_KEY,
    null,
    (v): v is ConsentChoice | null => v === null || isConsentChoice(v),
  );
  if (!choice) return null;
  // An expired choice is treated as no choice, so the banner re-asks.
  if (now - choice.ts > CONSENT_TTL_MS) return null;
  return choice;
}

/** Persist a choice, stamping it with `now`. */
export function writeConsent(
  choice: Pick<ConsentChoice, "analytics" | "marketing">,
  now: number,
): void {
  const record: ConsentChoice = {
    analytics: choice.analytics,
    marketing: choice.marketing,
    ts: now,
  };
  writePersisted(CONSENT_STORAGE_KEY, record);
}

/** Custom event the footer link dispatches to re-open the banner. */
export const CONSENT_OPEN_EVENT = "tbd:open-consent";

interface GtagWindow {
  gtag?: (...args: unknown[]) => void;
}

/** Push a Consent Mode `update` to gtag, if gtag is present. */
export function gtagConsentUpdate(
  choice: Pick<ConsentChoice, "analytics" | "marketing">,
): void {
  const w = window as unknown as GtagWindow;
  if (typeof w.gtag === "function") {
    w.gtag("consent", "update", toConsentModeUpdate(choice));
  }
}
