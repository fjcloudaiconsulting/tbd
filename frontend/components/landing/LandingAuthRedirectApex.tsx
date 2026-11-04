// LandingAuthRedirectApex.tsx — no-op stub used by the apex (S3 + CloudFront)
// build target. The apex host (thebetterdecision.com) has no session of its
// own: the refresh cookie is host-only on app.thebetterdecision.com. The two
// hosts are same-site, though, so a credentialed API call from apex script
// would carry that cookie (FE-01). Aliased in for the apex bundle by
// next.config.apex.ts so the apex output ships zero auth code.
//
// Must keep the same default export shape as LandingAuthRedirect so the
// landing page import resolves cleanly during the apex build.

export default function LandingAuthRedirectApex(): null {
  return null;
}
