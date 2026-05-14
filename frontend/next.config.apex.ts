import path from "node:path";
import type { NextConfig } from "next";

// next.config.apex.ts — secondary build target that produces a static
// export of the landing surface only, for upload to S3 + CloudFront on
// the apex host (thebetterdecision.com).
//
// The PRIMARY build (next.config.ts, `npm run build`) still produces the
// standalone Node bundle deployed to DigitalOcean App Platform on
// app.thebetterdecision.com. The two targets share the same Next.js app;
// build-target selection is via NEXT_PUBLIC_BUILD_TARGET=apex.
//
// Route allowlisting:
//   `output: 'export'` requires every route in `app/` to be statically
//   exportable. Authed routes use client-only hooks and would fail. The
//   `scripts/build-apex.sh` driver temporarily moves non-allowlisted
//   route directories out of `app/` for the duration of the build, then
//   restores them, then prunes any stragglers from `out-apex/`.
//
// Module substitutions:
//   - `@/components/landing/LandingAuthRedirect` -> no-op apex stub.
//   - `@/components/auth/AuthProvider`           -> no-op apex stub.
//   These keep auth code and the /me probe out of the apex bundle.
//
// Headers:
//   `output: 'export'` does NOT emit a Next.js server, so the `headers()`
//   contract from next.config.ts is ignored here. Security headers for
//   the apex host are configured at the CloudFront response-headers
//   policy (managed by PR-A Terraform).

// Aliases for Turbopack: project-relative paths starting with "./".
// Turbopack does NOT accept absolute filesystem paths here; it routes
// strings that start with "/" through its "server relative" resolver
// and fails. Keep these as "./components/..." style.
const TURBOPACK_APEX_ALIASES: Record<string, string> = {
  // Auth-island substitution (Option Y from PR brief). Mechanical alias
  // is simpler to audit than a build-time conditional import.
  "@/components/landing/LandingAuthRedirect":
    "./components/landing/LandingAuthRedirectApex.tsx",
  // Drop the AuthProvider + /me probe + token refresher from the apex
  // bundle. The stub re-exports useAuth / MfaRequiredError so any
  // landing component that pulls them in still type-checks even though
  // the apex render never invokes them.
  "@/components/auth/AuthProvider":
    "./components/auth/AuthProviderApex.tsx",
};

// Webpack expects absolute paths; mirror the same logical aliases.
const WEBPACK_APEX_ALIASES: Record<string, string> = {
  "@/components/landing/LandingAuthRedirect": path.resolve(
    __dirname,
    "components/landing/LandingAuthRedirectApex.tsx",
  ),
  "@/components/auth/AuthProvider": path.resolve(
    __dirname,
    "components/auth/AuthProviderApex.tsx",
  ),
};

const apexConfig: NextConfig = {
  output: "export",
  trailingSlash: true,
  // CloudFront serves images raw; we don't run the Next image optimizer.
  images: { unoptimized: true },
  poweredByHeader: false,
  // Distribute landing assets to a SEPARATE out directory so the standard
  // `npm run build` artefacts under `.next/` / `out/` are untouched.
  distDir: ".next-apex",
  // Next.js 16 defaults to Turbopack. Configure Turbopack's resolveAlias
  // so the apex-target stubs land in the bundle.
  turbopack: {
    resolveAlias: TURBOPACK_APEX_ALIASES,
  },
  // Webpack fallback for `next build --webpack` runs. Logically mirrors
  // the Turbopack aliases via WEBPACK_APEX_ALIASES (absolute paths).
  webpack: (config) => {
    config.resolve = config.resolve || {};
    config.resolve.alias = {
      ...(config.resolve.alias || {}),
      ...WEBPACK_APEX_ALIASES,
    };
    return config;
  },
};

export default apexConfig;
