#!/usr/bin/env bash
# build-apex.sh — produce a static export of the landing surface only,
# suitable for upload to S3 + CloudFront on the apex host.
#
# Strategy:
#   1. Move every non-allowlisted route directory out of `app/` into a
#      sibling staging dir so `next build` sees only the landing surface.
#   2. Swap next.config.ts <-> next.config.apex.ts for the duration of
#      the build. Next.js 16 does not expose a --config flag, so the
#      config file at the project root is the only knob. The apex config
#      sets `output: 'export'` and aliases the auth-island and
#      AuthProvider to no-op stubs.
#   3. Run `next build` with NEXT_PUBLIC_BUILD_TARGET=apex.
#   4. ALWAYS restore the original config and the staged routes, even on
#      failure (trap on EXIT / INT / TERM).
#   5. Move `out/` -> `out-apex/`, sanity-prune any non-allowlisted paths
#      that snuck through, and emit `_meta.json` with the build commit
#      SHA + timestamp for invalidation tracking.
#
# Output: frontend/out-apex/
# Consumed by: PR-B's GitHub Actions workflow (aws s3 sync).

set -euo pipefail

# Resolve frontend dir relative to this script (works from any cwd).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${FRONTEND_DIR}"

# Route directories under `app/` that must NOT ship to the apex build.
# Anything not on this list is either an allowlisted route (privacy,
# terms, docs) or a structural file (layout.tsx, page.tsx, globals.css,
# robots.ts, sitemap.ts, icons, error/loading/not-found, opengraph-image).
EXCLUDED_ROUTE_DIRS=(
  "accept-invite"
  "accounts"
  "admin"
  "auth"
  "budgets"
  "categories"
  "dashboard"
  "forecast-plans"
  "forgot-password"
  "health"
  "import"
  "login"
  "mfa-verify"
  "profile"
  "recurring"
  "register"
  "reset-password"
  "settings"
  "setup"
  "system"
  "transactions"
  "verify-email"
)

# Top-level files under `app/` that use dynamic rendering (e.g.
# ImageResponse) and would force `output: 'export'` to fail with
# "force-static not configured" unless we add force-static at the
# source. We stage them out for the apex build to keep the standard
# app build untouched. PR-B / a follow-up may swap in a static PNG OG
# fallback for the apex domain.
EXCLUDED_ROUTE_FILES=(
  "opengraph-image.tsx"
  "apple-icon.tsx"
  # sitemap.ts + robots.ts are MetadataRoute handlers that Next treats as
  # dynamic under `output: 'export'`. We stage them out and write static
  # replacements directly into out-apex/ at the end of the build.
  "sitemap.ts"
  "robots.ts"
)

# Top-level paths inside out-apex/ that are allowed to remain after build.
# (Each is a directory created by an allowlisted route, or a static asset.)
ALLOWED_OUTPUT_PATHS=(
  "index.html"
  "index.txt"
  "404.html"
  "_not-found"
  "privacy"
  "terms"
  "docs"
  "_next"
  "_meta.json"
  "robots.txt"
  "sitemap.xml"
  "icon.svg"
  "apple-icon.png"
  "opengraph-image.png"
  "favicon.ico"
  # Next 16 emits "__next.*.txt" RSC prefetch payloads alongside the HTML
  # routes. Keeping them silences harmless console 404s from <Link>
  # prefetch on hover.
  "__next.__PAGE__.txt"
  "__next._full.txt"
  "__next._head.txt"
  "__next._index.txt"
  "__next._tree.txt"
)

STAGING_DIR="${FRONTEND_DIR}/.apex-staged-routes"
CONFIG_FILE="${FRONTEND_DIR}/next.config.ts"
APEX_CONFIG_FILE="${FRONTEND_DIR}/next.config.apex.ts"
CONFIG_BACKUP="${FRONTEND_DIR}/.next.config.ts.bak"

restore_routes() {
  # Idempotent. Move anything we staged back into app/.
  if [[ -d "${STAGING_DIR}" ]]; then
    for d in "${STAGING_DIR}"/*; do
      [[ -e "${d}" ]] || continue
      local name
      name="$(basename "${d}")"
      if [[ -e "${FRONTEND_DIR}/app/${name}" ]]; then
        echo "build-apex: WARN restore target already exists for ${name}; leaving staged copy at ${d}" >&2
      else
        mv "${d}" "${FRONTEND_DIR}/app/${name}"
      fi
    done
    rmdir "${STAGING_DIR}" 2>/dev/null || true
  fi
  # Restore the standard next.config.ts if we swapped it.
  if [[ -f "${CONFIG_BACKUP}" ]]; then
    mv "${CONFIG_BACKUP}" "${CONFIG_FILE}"
  fi
}

# Trap covers normal exit, errors, and interrupts. Always restores.
trap restore_routes EXIT INT TERM

echo "build-apex: staging non-allowlisted routes out of app/"
mkdir -p "${STAGING_DIR}"
for dir in "${EXCLUDED_ROUTE_DIRS[@]}"; do
  src="${FRONTEND_DIR}/app/${dir}"
  if [[ -d "${src}" ]]; then
    mv "${src}" "${STAGING_DIR}/${dir}"
  fi
done
for file in "${EXCLUDED_ROUTE_FILES[@]}"; do
  src="${FRONTEND_DIR}/app/${file}"
  if [[ -f "${src}" ]]; then
    mv "${src}" "${STAGING_DIR}/${file}"
  fi
done

echo "build-apex: swapping next.config.ts -> next.config.apex.ts"
if [[ ! -f "${APEX_CONFIG_FILE}" ]]; then
  echo "build-apex: ERROR apex config not found at ${APEX_CONFIG_FILE}" >&2
  exit 1
fi
mv "${CONFIG_FILE}" "${CONFIG_BACKUP}"
cp "${APEX_CONFIG_FILE}" "${CONFIG_FILE}"

echo "build-apex: running next build with apex config"
rm -rf "${FRONTEND_DIR}/.next-apex" "${FRONTEND_DIR}/out" "${FRONTEND_DIR}/out-apex"

# NEXT_PUBLIC_SITE_URL drives canonical URLs and og:image / og:url meta
# tags from lib/site.ts. On apex it MUST point at the apex host so the
# rendered HTML doesn't claim canonical = app.thebetterdecision.com.
# Override via env when invoking the script for a non-prod apex host.
: "${NEXT_PUBLIC_SITE_URL:=https://thebetterdecision.com}"
export NEXT_PUBLIC_SITE_URL

NEXT_PUBLIC_BUILD_TARGET=apex \
  npx next build

# Next 16 with `output: 'export'` + a custom `distDir` writes the static
# export DIRECTLY into the distDir (here `.next-apex/`). Earlier Next
# versions wrote to `out/`. Handle both shapes.
mkdir -p "${FRONTEND_DIR}/out-apex"
if [[ -d "${FRONTEND_DIR}/out" ]]; then
  # Legacy behaviour: export lives in `out/`.
  mv "${FRONTEND_DIR}/out"/* "${FRONTEND_DIR}/out-apex"/ 2>/dev/null || true
  rm -rf "${FRONTEND_DIR}/out"
fi
if [[ -f "${FRONTEND_DIR}/.next-apex/index.html" ]]; then
  # Next 16 behaviour: static export shares the distDir. Copy the
  # public-facing artefacts and ignore Next's internal build files.
  for entry in "${FRONTEND_DIR}/.next-apex"/*; do
    name="$(basename "${entry}")"
    # Skip Next.js INTERNAL scaffolding (build-time metadata, dev caches,
    # server runtime files) that lives under distDir but is not part of
    # the static export surface CloudFront needs to serve. Note we DO
    # keep `__next.*.txt` files — those are RSC prefetch payloads that
    # Next/Link fetches on hover; removing them produces harmless but
    # noisy 404s in the browser devtools.
    case "${name}" in
      server|trace|build-manifest.json|app-build-manifest.json|prerender-manifest.json|routes-manifest.json|images-manifest.json|next-minimal-server.js.nft.json|next-server.js.nft.json|export-marker.json|export-detail.json|required-server-files.json|BUILD_ID|package.json|cache|diagnostics|static)
        continue
        ;;
    esac
    cp -R "${entry}" "${FRONTEND_DIR}/out-apex/${name}"
  done
fi

if [[ ! -f "${FRONTEND_DIR}/out-apex/index.html" ]]; then
  echo "build-apex: ERROR no index.html in out-apex/, build appears to have failed" >&2
  exit 1
fi

echo "build-apex: pruning any non-allowlisted output paths"
shopt -s nullglob
declare -A ALLOWED
for entry in "${ALLOWED_OUTPUT_PATHS[@]}"; do
  ALLOWED["${entry}"]=1
done
for entry in "${FRONTEND_DIR}/out-apex"/*; do
  name="$(basename "${entry}")"
  if [[ -z "${ALLOWED[${name}]:-}" ]]; then
    echo "build-apex:   pruning unexpected output: ${name}"
    rm -rf "${entry}"
  fi
done
shopt -u nullglob

COMMIT_SHA="$(git -C "${FRONTEND_DIR}" rev-parse HEAD 2>/dev/null || echo unknown)"
BUILD_TIME="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
APEX_URL="${NEXT_PUBLIC_APEX_URL:-https://thebetterdecision.com}"
APP_URL="${NEXT_PUBLIC_APP_URL:-https://app.thebetterdecision.com}"

cat > "${FRONTEND_DIR}/out-apex/_meta.json" <<EOF
{
  "commit": "${COMMIT_SHA}",
  "built_at": "${BUILD_TIME}",
  "target": "apex",
  "host": "thebetterdecision.com"
}
EOF

# Static SEO replacements for the routes staged out of the build.
# robots.txt — allow indexing of the apex landing surface, point at the
# apex sitemap (NOT the app sitemap; the app keeps its own at
# app.thebetterdecision.com/sitemap.xml).
cat > "${FRONTEND_DIR}/out-apex/robots.txt" <<EOF
User-agent: *
Allow: /

Sitemap: ${APEX_URL}/sitemap.xml
EOF

# sitemap.xml — list only the apex-exported routes.
cat > "${FRONTEND_DIR}/out-apex/sitemap.xml" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>${APEX_URL}/</loc><lastmod>${BUILD_TIME%T*}</lastmod></url>
  <url><loc>${APEX_URL}/privacy/</loc><lastmod>${BUILD_TIME%T*}</lastmod></url>
  <url><loc>${APEX_URL}/terms/</loc><lastmod>${BUILD_TIME%T*}</lastmod></url>
  <url><loc>${APEX_URL}/docs/</loc><lastmod>${BUILD_TIME%T*}</lastmod></url>
</urlset>
EOF

echo "build-apex: done"
echo "build-apex: output at ${FRONTEND_DIR}/out-apex"
ls -la "${FRONTEND_DIR}/out-apex"
