// links.ts — single source of truth for cross-domain CTA URLs used by the
// landing surface (TopNav, Hero, SecondCta, LandingFooter).
//
// The app build (target = DigitalOcean App Platform) serves the landing and
// the authed app from the same origin (`app.thebetterdecision.com`), so
// `/register` and `/login` resolve correctly as relative paths.
//
// The apex build (target = AWS S3 + CloudFront, host = `thebetterdecision.com`)
// serves ONLY the landing surface; auth lives on a different host. Same-origin
// cookies do not cross the host boundary, so we MUST link absolutely to the
// app host for sign-in and sign-up.
//
// Selection is via NEXT_PUBLIC_BUILD_TARGET, set at build time:
//   - "apex"  -> CTAs are absolute URLs to BRAND_APP_URL
//   - unset / anything else -> CTAs are relative paths (existing behaviour)
//
// BRAND_APP_URL defaults to https://app.thebetterdecision.com but can be
// overridden via NEXT_PUBLIC_APP_URL (no trailing slash). This lets the apex
// build target a staging app host if needed.

const rawAppUrl = (process.env.NEXT_PUBLIC_APP_URL || "").trim();
export const BRAND_APP_URL = (rawAppUrl || "https://app.thebetterdecision.com").replace(/\/$/, "");

export const IS_APEX_BUILD = process.env.NEXT_PUBLIC_BUILD_TARGET === "apex";

function joinAppUrl(path: string): string {
  if (!path.startsWith("/")) {
    return `${BRAND_APP_URL}/${path}`;
  }
  return `${BRAND_APP_URL}${path}`;
}

// Resolve a CTA path for the current build target. Path must be a relative
// in-app path (e.g. "/register"). For non-apex builds, returns the path
// unchanged so Next's <Link> handles client-side navigation. For apex
// builds, returns an absolute URL pointing at BRAND_APP_URL.
export function ctaHref(path: string): string {
  if (IS_APEX_BUILD) {
    return joinAppUrl(path);
  }
  return path;
}

export const signupHref = (): string => ctaHref("/register");
export const signinHref = (): string => ctaHref("/login");
