// GA4 measurement config. GA loads ONLY on the apex marketing build; the
// authenticated app host never renders it (see GoogleAnalytics.tsx).
export const GA_MEASUREMENT_ID =
  process.env.NEXT_PUBLIC_GA_MEASUREMENT_ID || "G-GRXDVTVBLV";

export const isApexBuild = process.env.NEXT_PUBLIC_BUILD_TARGET === "apex";
