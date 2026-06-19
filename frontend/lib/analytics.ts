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
