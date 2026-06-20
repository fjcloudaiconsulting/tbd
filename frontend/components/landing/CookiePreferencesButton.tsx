"use client";

import { isApexBuild } from "@/lib/analytics";
import { CONSENT_OPEN_EVENT } from "@/lib/consent";

// Footer entry point to re-open the consent banner so a visitor can change or
// withdraw consent as easily as they gave it (GDPR). Dispatches the event the
// apex-mounted ConsentBanner listens for. Renders only on the apex build —
// where the banner exists — so it never appears as a dead control on the
// authenticated app host (which runs no GA and mounts no banner).
export default function CookiePreferencesButton() {
  if (!isApexBuild) return null;
  return (
    <button
      type="button"
      className="hover:text-text-primary"
      onClick={() => window.dispatchEvent(new Event(CONSENT_OPEN_EVENT))}
    >
      Cookie preferences
    </button>
  );
}
