"use client";

import { CONSENT_OPEN_EVENT } from "@/lib/consent";

// Footer entry point to re-open the consent banner so a visitor can change or
// withdraw consent as easily as they gave it (GDPR). Dispatches the event the
// apex-mounted ConsentBanner listens for.
export default function CookiePreferencesButton() {
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
