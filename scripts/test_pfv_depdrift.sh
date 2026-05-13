#!/usr/bin/env bash
# Test harness for `check_frontend_dep_drift` in `./pfv`.
#
# We can't `source` the full pfv script (the bottom `case` dispatches
# on $1). Instead, we extract the function block and source that.
#
# Run from the repo root:
#   bash scripts/test_pfv_depdrift.sh
#
# Exit code 0 on success, non-zero on assertion failure.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PFV_SCRIPT="$REPO_ROOT/pfv"

if [[ ! -f "$PFV_SCRIPT" ]]; then
  echo "FAIL: $PFV_SCRIPT not found"
  exit 1
fi

# Extract `check_frontend_dep_drift` (and only that function) by reading
# from its opening line through its closing `}` at column 1. The function
# is self-contained, no helpers needed.
tmp_fn="$(mktemp -t pfv_depdrift.XXXXXX.sh)"
trap 'rm -f "$tmp_fn"' EXIT

awk '
  /^check_frontend_dep_drift\(\) \{/ { capture = 1 }
  capture { print }
  capture && /^\}$/ { capture = 0 }
' "$PFV_SCRIPT" > "$tmp_fn"

if ! grep -q "check_frontend_dep_drift" "$tmp_fn"; then
  echo "FAIL: could not extract check_frontend_dep_drift from $PFV_SCRIPT"
  exit 1
fi

# shellcheck disable=SC1090
source "$tmp_fn"

pass=0
fail=0

assert_no_warning() {
  local name="$1"; shift
  local out
  out="$("$@" 2>&1 >/dev/null || true)"
  if [[ -z "$out" ]]; then
    echo "PASS: $name"
    pass=$((pass + 1))
  else
    echo "FAIL: $name — expected no output, got: $out"
    fail=$((fail + 1))
  fi
}

assert_warning_matches() {
  local name="$1"; shift
  local needle="$1"; shift
  local out
  out="$("$@" 2>&1 >/dev/null || true)"
  if [[ "$out" == *"$needle"* ]]; then
    echo "PASS: $name"
    pass=$((pass + 1))
  else
    echo "FAIL: $name — expected warning containing '$needle', got: $out"
    fail=$((fail + 1))
  fi
}

# The function reads `frontend/package-lock.json` from $PWD. Run the
# tests from a tempdir that has an empty placeholder lockfile so the
# host-hash path is exercised when overrides are NOT set.
work_dir="$(mktemp -d -t pfv_depdrift_work.XXXXXX)"
trap 'rm -rf "$work_dir"; rm -f "$tmp_fn"' EXIT
mkdir -p "$work_dir/frontend"
echo "{}" > "$work_dir/frontend/package-lock.json"
cd "$work_dir"

# 1. Matching hashes — no warning.
assert_no_warning "matching hashes emit no warning" \
  env PFV_DEPDRIFT_HOST_HASH=abc123 PFV_DEPDRIFT_CONTAINER_HASH=abc123 \
  bash -c "source '$tmp_fn'; check_frontend_dep_drift"

# 2. Differing hashes — warning fires.
assert_warning_matches "differing hashes emit drift warning" \
  "host frontend/package-lock.json differs" \
  env PFV_DEPDRIFT_HOST_HASH=abc123 PFV_DEPDRIFT_CONTAINER_HASH=def456 \
  bash -c "source '$tmp_fn'; check_frontend_dep_drift"

# 3. PFV_DEPDRIFT_SKIP=1 silences even when hashes differ.
assert_no_warning "PFV_DEPDRIFT_SKIP=1 silences mismatched hashes" \
  env PFV_DEPDRIFT_SKIP=1 PFV_DEPDRIFT_HOST_HASH=abc123 PFV_DEPDRIFT_CONTAINER_HASH=def456 \
  bash -c "source '$tmp_fn'; check_frontend_dep_drift"

# 4. Missing frontend/package-lock.json — silent skip.
rm -f "$work_dir/frontend/package-lock.json"
assert_no_warning "missing package-lock.json skips silently" \
  bash -c "source '$tmp_fn'; check_frontend_dep_drift"
echo "{}" > "$work_dir/frontend/package-lock.json"  # restore for any future checks

echo ""
echo "==================="
echo "Passed: $pass"
echo "Failed: $fail"
echo "==================="

if (( fail > 0 )); then
  exit 1
fi
exit 0
