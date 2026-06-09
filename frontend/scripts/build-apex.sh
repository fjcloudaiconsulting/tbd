#!/usr/bin/env bash
# build-apex.sh — produce a static export of the landing surface only,
# suitable for upload to S3 + CloudFront on the apex host.
#
# Strategy:
#   1. Walk every entry under `app/` and decide via a positive allowlist
#      whether it ships to the apex build. Anything not on the allowlist
#      is moved to a sibling staging dir so `next build` doesn't see it.
#      This defaults to deny: any route added in the future (e.g.
#      /onboarding, PR #238) is automatically staged out unless someone
#      explicitly adds it to the allowlist here.
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
#   6. Run post-build guards that hard-fail the build if the output
#      contains an unexpected top-level entry or any `/api/v1` reference
#      (would mean auth/backend code leaked into the apex bundle).
#
# Compatibility: this script MUST run on macOS /bin/bash (3.2.57). That
# means NO `declare -A`, NO `mapfile`/`readarray`, NO `${var^^}`-style
# case-conversion expansions, NO `&>>`. Use plain indexed arrays, case
# statements, and `while IFS= read -r line; do ...; done < <(cmd)` for
# line iteration.
#
# Output: frontend/out-apex/
# Consumed by: PR-B's GitHub Actions workflow (aws s3 sync).

set -euo pipefail

# Resolve frontend dir relative to this script (works from any cwd).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${FRONTEND_DIR}"

# Positive allowlist: route directories under `app/` that DO ship to the
# apex build. Everything else under `app/` (auth, dashboard, admin, the
# whole authed surface) is staged out for the duration of the build.
ALLOWED_ROUTE_DIRS=(
  "privacy"
  "terms"
  "docs"
  "features"
  "compare"
  "vs"
)

# Positive allowlist: app-level files (siblings of route directories)
# that DO ship to the apex build. These are the framework structural
# files plus the landing page itself. Files NOT on this list (such as
# opengraph-image.tsx, apple-icon.tsx, sitemap.ts, robots.ts, manifest.ts)
# are dynamic and would force a server runtime, so we stage them out and
# write static replacements directly into out-apex/ at the end of the
# build.
ALLOWED_APP_FILES=(
  "layout.tsx"
  "page.tsx"
  "globals.css"
  "error.tsx"
  "not-found.tsx"
  "loading.tsx"
  "global-error.tsx"
  "icon.svg"
)

# Top-level paths inside out-apex/ that are allowed after the build.
# Glob-style matching via `case` (so `__next.*.txt` works for RSC
# prefetch payloads). Used by the post-build guard.
ALLOWED_OUTPUT_GLOBS=(
  "index.html"
  "index.txt"
  "404"
  "404.html"
  "_not-found"
  "privacy"
  "terms"
  "docs"
  "features"
  "compare"
  "vs"
  "_next"
  "_meta.json"
  "robots.txt"
  "sitemap.xml"
  "icon.svg"
  "favicon.ico"
  # Static social-share image (public/og.png), referenced as og:image /
  # twitter:image by lib/site.ts. Copied verbatim from public/ into the
  # export; the dynamic /opengraph-image route is not exported here.
  "og.png"
  # Static llms.txt (public/llms.txt) describing the product + key apex
  # pages for AI crawlers / answer engines. Copied verbatim from public/
  # into the export; must be on this allowlist or the post-build guard
  # rejects it (same rule that applied to og.png).
  "llms.txt"
  "__next.*.txt"
  # Reserved directory under public/ for future marketing screenshots
  # used by the landing surface. Next.js copies the whole public/ tree
  # into the build output, so the placeholder folder (currently just a
  # .gitkeep) lands here. Allow it through so the apex post-build guard
  # doesn't trip when polished screenshots get dropped in later.
  "screenshots"
)

STAGING_DIR="${FRONTEND_DIR}/.apex-staged-routes"
CONFIG_FILE="${FRONTEND_DIR}/next.config.ts"
APEX_CONFIG_FILE="${FRONTEND_DIR}/next.config.apex.ts"
CONFIG_BACKUP="${FRONTEND_DIR}/.next.config.ts.bak"

# Bash 3.2 has no associative arrays. Use a helper that does an O(n) scan
# of an indexed array. Callers pass the needle then the array elements
# expanded at the call site (Bash 3.2 has no namerefs either).
contains() {
  # Usage: contains "needle" "${array[@]}"
  local needle="$1"
  shift
  local item
  for item in "$@"; do
    if [[ "${item}" = "${needle}" ]]; then
      return 0
    fi
  done
  return 1
}

# Returns 0 if `name` matches any of the ALLOWED_OUTPUT_GLOBS (with
# shell-style wildcard matching via `case`). Bash 3.2 safe.
output_allowed() {
  local name="$1"
  local pattern
  for pattern in "${ALLOWED_OUTPUT_GLOBS[@]}"; do
    case "${name}" in
      ${pattern}) return 0 ;;
    esac
  done
  return 1
}

restore_routes() {
  # Idempotent. Move anything we staged back into app/.
  local d name
  if [[ -d "${STAGING_DIR}" ]]; then
    for d in "${STAGING_DIR}"/*; do
      [[ -e "${d}" ]] || continue
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

echo "build-apex: staging non-allowlisted entries out of app/ (default-deny allowlist)"
mkdir -p "${STAGING_DIR}"

# Walk every entry under app/ and stage out anything not on the
# positive allowlist. Default-deny means new routes (e.g. PR #238's
# /onboarding) are staged out automatically until someone explicitly
# adds them to ALLOWED_ROUTE_DIRS / ALLOWED_APP_FILES.
shopt -s nullglob
for entry in "${FRONTEND_DIR}/app"/*; do
  name="$(basename "${entry}")"
  if [[ -d "${entry}" ]]; then
    if contains "${name}" "${ALLOWED_ROUTE_DIRS[@]}"; then
      continue
    fi
  elif [[ -f "${entry}" ]]; then
    if contains "${name}" "${ALLOWED_APP_FILES[@]}"; then
      continue
    fi
  else
    # Symlink, socket, etc. Leave alone.
    continue
  fi
  echo "build-apex:   staging out app/${name}"
  mv "${entry}" "${STAGING_DIR}/${name}"
done
shopt -u nullglob

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

COMMIT_SHA="$(git -C "${FRONTEND_DIR}" rev-parse HEAD 2>/dev/null || echo unknown)"
BUILD_TIME="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
APEX_URL="${NEXT_PUBLIC_APEX_URL:-https://thebetterdecision.com}"
APP_URL="${NEXT_PUBLIC_APP_URL:-https://app.thebetterdecision.com}"

# Static SEO replacements for the routes staged out of the build.
# robots.txt — allow indexing of the apex landing surface, point at the
# apex sitemap (NOT the app sitemap; the app keeps its own at
# app.thebetterdecision.com/sitemap.xml).
#
# The apex host serves ONLY public marketing + docs content, so we
# explicitly welcome the major AI crawlers / answer engines (training
# and live-retrieval bots) in addition to the catch-all. The app host
# (app.thebetterdecision.com) stays auth-walled and keeps its own
# noindex robots from app/robots.ts; do not loosen that one.
cat > "${FRONTEND_DIR}/out-apex/robots.txt" <<EOF
User-agent: GPTBot
Allow: /

User-agent: ChatGPT-User
Allow: /

User-agent: OAI-SearchBot
Allow: /

User-agent: ClaudeBot
Allow: /

User-agent: Claude-Web
Allow: /

User-agent: anthropic-ai
Allow: /

User-agent: PerplexityBot
Allow: /

User-agent: Google-Extended
Allow: /

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
  <url><loc>${APEX_URL}/docs/plans/</loc><lastmod>${BUILD_TIME%T*}</lastmod></url>
  <url><loc>${APEX_URL}/features/</loc><lastmod>${BUILD_TIME%T*}</lastmod></url>
  <url><loc>${APEX_URL}/compare/</loc><lastmod>${BUILD_TIME%T*}</lastmod></url>
  <url><loc>${APEX_URL}/vs/spreadsheets/</loc><lastmod>${BUILD_TIME%T*}</lastmod></url>
  <url><loc>${APEX_URL}/vs/ynab/</loc><lastmod>${BUILD_TIME%T*}</lastmod></url>
</urlset>
EOF

# Post-build guards (belt-and-suspenders behind the input allowlist).
# Run BEFORE writing _meta.json so a poisoned bundle never gets a
# "successful build" marker.
echo "build-apex: post-build guard — verifying out-apex/ top-level entries"
shopt -s nullglob
guard_failures=0
for entry in "${FRONTEND_DIR}/out-apex"/*; do
  name="$(basename "${entry}")"
  if ! output_allowed "${name}"; then
    echo "build-apex: GUARD FAIL unexpected top-level entry: ${name}" >&2
    guard_failures=$((guard_failures + 1))
  fi
done
shopt -u nullglob
if [[ "${guard_failures}" -gt 0 ]]; then
  echo "build-apex: ERROR ${guard_failures} unexpected entries in out-apex/. Aborting." >&2
  exit 1
fi

echo "build-apex: post-build guard — grepping for /api/v1 references in built output"
# grep -r returns 0 if matches found, 1 if no matches, 2 on error. We
# want to act on the result, so capture it explicitly (set -e would kill
# us on the "1 = no match" return otherwise).
api_hits="$(grep -rl "/api/v1" "${FRONTEND_DIR}/out-apex" 2>/dev/null || true)"
if [[ -n "${api_hits}" ]]; then
  echo "build-apex: GUARD FAIL /api/v1 reference leaked into apex bundle:" >&2
  echo "${api_hits}" >&2
  echo "build-apex: ERROR auth/backend code leaked into apex bundle. Aborting." >&2
  exit 1
fi

cat > "${FRONTEND_DIR}/out-apex/_meta.json" <<EOF
{
  "commit": "${COMMIT_SHA}",
  "built_at": "${BUILD_TIME}",
  "target": "apex",
  "host": "thebetterdecision.com"
}
EOF

echo "build-apex: done"
echo "build-apex: output at ${FRONTEND_DIR}/out-apex"
ls -la "${FRONTEND_DIR}/out-apex"
