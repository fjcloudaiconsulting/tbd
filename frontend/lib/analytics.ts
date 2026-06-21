// GA4 measurement config. GA loads ONLY on the apex marketing build; the
// authenticated app host never renders it (see GoogleAnalytics.tsx).
export const GA_MEASUREMENT_ID =
  process.env.NEXT_PUBLIC_GA_MEASUREMENT_ID || "G-GRXDVTVBLV";

// First-party measurement path for the Google tag gateway. With the gateway,
// the gtag.js loader (and all GA collection) is served from our own domain
// under this path, which CloudFront proxies to G-GRXDVTVBLV.fps.goog. The
// loader src therefore points here instead of googletagmanager.com, so the
// browser never makes a third-party request and the apex CSP needs no GA
// external origins ('self' covers it). Must match the CloudFront behavior's
// path pattern (/vd9r/*).
export const GA_GATEWAY_PATH =
  process.env.NEXT_PUBLIC_GA_GATEWAY_PATH || "/vd9r/";

export const isApexBuild = process.env.NEXT_PUBLIC_BUILD_TARGET === "apex";

export type SignupCtaLocation = "hero" | "topnav" | "second_cta" | "vs_page";

type GtagFn = (command: string, ...args: unknown[]) => void;

// Fire a GA4 event when a visitor clicks a signup CTA. The operator imports
// this event into Google Ads as the "register_click" conversion. GA4's
// sendBeacon transport lets the event survive the cross-domain navigation to
// the app host, so we never delay navigation. Consent Mode (bootstrapped in
// GoogleAnalytics.tsx) handles redaction — do not gate on consent here.
export function trackRegisterClick(location: SignupCtaLocation): void {
  if (!isApexBuild || typeof window === "undefined") return;
  const gtag = (window as unknown as { gtag?: GtagFn }).gtag;
  if (typeof gtag !== "function") return;
  gtag("event", "register_click", { cta_location: location });
}
