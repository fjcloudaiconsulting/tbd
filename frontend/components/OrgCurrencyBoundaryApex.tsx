// OrgCurrencyBoundaryApex.tsx — no-op OrgCurrencyBoundary stub for the apex
// (S3 + CloudFront) build target. Aliased in by next.config.apex.ts, mirroring
// the AuthProvider stub beside it.
//
// ⚠ WHY THIS FILE EXISTS, so nobody deletes it as dead code.
// TBD-503 mounted `OrgCurrencyBoundary` in the ROOT layout, which the apex
// marketing pages share. The real boundary pulls in `useAuth` and
// `use-org-currency` -> `use-accounts` -> `lib/api`, and `lib/api` carries
// `/api/v1/...` path literals. `scripts/build-apex.sh`'s post-build guard
// greps `out-apex/` for `/api/v1` and aborts the build, which is exactly what
// it did: the apex export failed in CI while every local gate was green,
// because the apex target is only built there.
//
// The apex host serves landing pages only. It has no session, no accounts and
// no money figures, so there is no currency to resolve and the passthrough
// costs nothing.
//
// The default export is what `app/layout.tsx` imports; keep the shape
// identical if the real boundary's signature changes.

export default function OrgCurrencyBoundaryApex({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
