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
# This check is wired into CI (.github/workflows/test.yml) and is expected to
# PASS: the call sites have been migrated to theme tokens. Both the raw-palette
# / hex checks and the phantom-token check (a className referencing a --color-*
# name that does not exist, which silently emits no CSS) are fatal. Keep it
# green: use a theme token from app/globals.css or a primitive from
# lib/styles.ts instead of a raw color or an undefined token.
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
#   - components/auth/GoogleSSOButton.tsx — Google's official SVG mark
#     uses Google's locked 4-color brand hexes (#4285F4 / #34A853 /
#     #FBBC05 / #EA4335) and provider-spec surface colors that MUST NOT
#     theme-switch. Google's branding guide forbids recolor.
EXCLUDES=(
  --exclude-dir=node_modules
  --exclude-dir=.next
  --exclude-dir=tests
  --exclude=opengraph-image.tsx
  --exclude=apple-icon.tsx
  --exclude=global-error.tsx
  --exclude=brand.ts
  --exclude=GoogleSSOButton.tsx
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

# ── Phantom theme-token utilities ────────────────────────────────────
#
# Catches Tailwind classes that READ like a project token (e.g.
# ``bg-bg-elevated`` or ``hover:text-text``) but reference a name that
# does not exist as ``--color-<name>`` in ``app/globals.css``. These
# are silent: Tailwind emits no rule for an unknown utility, so the
# element renders without styling and the bug ships invisibly.
#
# The check is scoped to OUR project prefixes (bg, surface, border,
# text, accent, success, danger, warning, sidebar) so Tailwind's own
# compound utilities (``text-balance``, ``border-collapse``,
# ``bg-clip``, etc.) are never flagged. ``text-primary`` would slip
# through (no hyphen after ``primary``), so the regex requires the
# project namespace to be the SECOND segment of a 2+-segment class.
#
# Added 2026-05-13 after L4.4 cross-org user search shipped phantom
# ``bg-bg-elevated`` and ``border-border-strong`` utilities (PR #257
# review comment).
PROJECT_NAMESPACES='(bg|surface|border|text|accent|success|danger|warning|sidebar)'
PHANTOM_RE="\\b(hover:)?(bg|text|border|ring|fill|stroke|placeholder|caret|outline|divide|shadow|from|to|via|decoration)-${PROJECT_NAMESPACES}(-[a-z0-9]+)+\\b"

# Harvest the catalog from globals.css. Each --color-<name> line gives
# us a valid token suffix. ``--color-bg`` and ``--color-text-primary``
# become catalog entries ``bg`` and ``text-primary``.
CATALOG_FILE="$(mktemp)"
trap 'rm -f "${CATALOG_FILE}"' EXIT
grep -E '^\s+--color-[a-z0-9-]+:' app/globals.css \
  | sed -E 's/^\s+--color-([a-z0-9-]+):.*/\1/' \
  | sort -u > "${CATALOG_FILE}"

# Collect candidate phantom utilities from the codebase, normalize to
# the suffix we can compare against the catalog, then filter.
candidates="$(grep -rEnH "${PHANTOM_RE}" \
  --include='*.ts' --include='*.tsx' --include='*.js' --include='*.jsx' \
  "${EXCLUDES[@]}" "${ROOTS[@]}" 2>/dev/null || true)"

if [ -n "${candidates}" ]; then
  phantom_hits=""
  while IFS= read -r line; do
    # Extract every matching utility on the line; one line may carry
    # several classes inside a single string.
    while IFS= read -r token; do
      # Strip the optional ``hover:`` prefix + the Tailwind property
      # (``bg-``, ``text-``, etc.) so what remains is the token suffix
      # the catalog stores.
      suffix="$(echo "${token}" | sed -E 's/^(hover:)?(bg|text|border|ring|fill|stroke|placeholder|caret|outline|divide|shadow|from|to|via|decoration)-//')"
      if ! grep -qx "${suffix}" "${CATALOG_FILE}"; then
        phantom_hits+="${line}    (unknown token: ${token} -> --color-${suffix} missing)
"
        break  # one hit per line is enough; avoid noisy dupes.
      fi
    done < <(echo "${line}" | grep -oE "${PHANTOM_RE}")
  done <<< "${candidates}"

  if [ -n "${phantom_hits}" ]; then
    # Phantom-token findings are FATAL. All known offenders were migrated
    # to real tokens (audit P1/P2), so any new className referencing a
    # --color-* name that does not exist (it silently emits no CSS, e.g.
    # invisible text or a dead hover) must fail the check.
    fail=1
    echo "── Phantom theme-token utilities (no --color-* match) ─────"
    echo "FAIL: className references a token with no --color-* definition."
    printf '%s' "${phantom_hits}"
    echo
  fi
fi

if [ "${fail}" -ne 0 ]; then
  echo "Design-token check failed. Replace the offenders with theme tokens"
  echo "from app/globals.css (e.g. bg-warning, text-danger, etc.) or with"
  echo "primitives from lib/styles.ts (btnPrimary, badgeWarning, stickyBar...)."
  exit 1
fi

echo "Design-token check passed."
exit 0
