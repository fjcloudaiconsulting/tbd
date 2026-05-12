// LandingAuthRedirectApex.tsx — no-op stub used by the apex (S3 + CloudFront)
// build target. The apex host (thebetterdecision.com) does not share cookies
// with the app host (app.thebetterdecision.com), so the auth-redirect island
// could never fire there. Aliased in for the apex bundle by
// next.config.apex.ts so the apex output ships zero auth code.
//
// Must keep the same default export shape as LandingAuthRedirect so the
// landing page import resolves cleanly during the apex build.

export default function LandingAuthRedirectApex(): null {
  return null;
}
