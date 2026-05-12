#!/usr/bin/env bash
# check-design-tokens.sh — enforce the design system token discipline.
#
# Forbidden in app/, components/, lib/ (the runtime UI surface):
#   - Raw Tailwind palette utilities (bg-red-500, text-amber-600, etc.).
#   - text-white / text-black in .ts/.tsx (legitimate inline-style escape
#     hatches `app/opengraph-image.tsx` and `app/global-error.tsx` are
#     excluded — they cannot rely on Tailwind theme tokens at runtime).
#   - Hard-coded hex literals in .ts/.tsx.
#
# This script is EXPECTED TO FAIL today (Phase A): the foundation PR adds
# the missing primitives but does not migrate call sites. Phase B will
# replace the offending utilities at the call sites and, once green, this
# check will be wired into CI. Until then, do not gate CI on it.
#
# Tracked Phase B fix targets (non-exhaustive):
#   - app/transactions/page.tsx (sticky bar + amber/red utilities)
#   - components/categories/BatchActionBar.tsx (sticky bar)
#   - any component still using bg-amber-* / bg-red-* / text-white / text-black
#
# Usage:
#   bash frontend/scripts/check-design-tokens.sh
#
# Exits 0 when clean, 1 when any forbidden pattern is found.

set -uo pipefail

# Resolve frontend dir relative to this script so it works from any cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${FRONTEND_DIR}"

# Roots we scan.
ROOTS=(app components lib)

# Files / directories to exclude:
#   - tests/
#   - node_modules / .next (never present under app/components/lib but defensive)
#   - app/opengraph-image.tsx — Next.js OG image route, only inline styles work.
#   - app/apple-icon.tsx — Next.js dynamic icon route, inline styles only.
#   - app/global-error.tsx — root error boundary; runs without globals.css.
#   - lib/brand.ts — canonical brand constants, not theme tokens.
EXCLUDES=(
  --exclude-dir=node_modules
  --exclude-dir=.next
  --exclude-dir=tests
  --exclude=opengraph-image.tsx
  --exclude=apple-icon.tsx
  --exclude=global-error.tsx
  --exclude=brand.ts
)

PALETTES='(slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)'
PROPS='(bg|text|border|ring|fill|stroke|from|to|via|outline|divide|placeholder|caret|accent|shadow|decoration|hover:bg|hover:text|hover:border)'

PALETTE_RE="${PROPS}-${PALETTES}-[0-9]+"
WHITE_BLACK_RE='\b(text-white|text-black)\b'
# Match 6-char hex literals only. The 3-char shorthand is too easily
# confused with GitHub PR references (e.g. "PR #197") that pepper our
# comments. If you ever need to catch shorthand, tighten this in Phase B
# after the loud violations are gone.
HEX_RE='#[0-9a-fA-F]{6}\b'

fail=0

run_check() {
  local label="$1"
  local pattern="$2"
  shift 2
  local includes=("$@")

  local matches
  matches=$(grep -rEnH "${pattern}" "${includes[@]}" "${EXCLUDES[@]}" "${ROOTS[@]}" 2>/dev/null || true)
  if [ -n "${matches}" ]; then
    echo "── ${label} ─────────────────────────────"
    echo "${matches}"
    echo
    fail=1
  fi
}

run_check "Raw Tailwind palette utilities" "${PALETTE_RE}" \
  --include='*.ts' --include='*.tsx' --include='*.js' --include='*.jsx'

run_check "text-white / text-black (use tokens)" "${WHITE_BLACK_RE}" \
  --include='*.ts' --include='*.tsx'

run_check "Hard-coded hex literals" "${HEX_RE}" \
  --include='*.ts' --include='*.tsx'

if [ "${fail}" -ne 0 ]; then
  echo "Design-token check failed. Replace the offenders with theme tokens"
  echo "from app/globals.css (e.g. bg-warning, text-danger, etc.) or with"
  echo "primitives from lib/styles.ts (btnPrimary, badgeWarning, stickyBar...)."
  exit 1
fi

echo "Design-token check passed."
exit 0
