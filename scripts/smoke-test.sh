#!/usr/bin/env bash
# Post-deploy smoke tests (L0.5).
#
# Verifies the live app can serve traffic after a deploy. Runs from
# .github/workflows/deploy.yml after the DigitalOcean App Platform
# deploy step succeeds — DO marking a deploy ACTIVE is necessary but
# not sufficient. This script asserts the actual surface a real user
# would touch first.
#
# Surface (intentionally minimal — full E2E is project_functional_tests.md):
#   1. GET  /health                     — liveness
#   2. GET  /ready                      — readiness (DB + Redis reachable)
#   3. POST /api/v1/auth/login          — authenticated round-trip
#   4. GET  /api/v1/categories          — one authenticated read
#
# Inputs (env vars):
#   SMOKE_BASE_URL     — public URL, e.g. https://app.thebetterdecision.com
#   SMOKE_USERNAME     — login for the dedicated smoke user (NOT a real user)
#   SMOKE_PASSWORD     — password for the smoke user
#
# The smoke user must:
#   - live in a DEDICATED smoke-only org with no real financial data.
#     MEMBER is NOT read-only in this app — many data-plane routes (POST
#     /transactions, /accounts, /budgets, /categories) only require
#     get_current_user. A smoke credential with MFA off, sitting in
#     GitHub Actions secrets, MUST NOT have access to real customer or
#     household data. OWNER of an empty smoke org is fine; MEMBER of a
#     real customer org is NOT acceptable.
#   - have email_verified = True
#   - have MFA disabled (otherwise login returns a challenge, not a token)
#
# Exit codes:
#   0  every check passed
#   1  any check failed
#   2  required env var missing
#
# Token / cookie redaction: this script never prints the access_token,
# the refresh_token cookie, or the password. Failure logs include status
# code and a small body excerpt only — never the request payload.

set -uo pipefail

# ── Inputs ──────────────────────────────────────────────────────────────────

BASE_URL="${SMOKE_BASE_URL:-}"
USERNAME="${SMOKE_USERNAME:-}"
PASSWORD="${SMOKE_PASSWORD:-}"

if [[ -z "$BASE_URL" ]]; then
  echo "✗ SMOKE_BASE_URL is not set"
  exit 2
fi
if [[ -z "$USERNAME" || -z "$PASSWORD" ]]; then
  echo "✗ SMOKE_USERNAME and SMOKE_PASSWORD must both be set"
  exit 2
fi

# Strip any trailing slash so we can safely concat path suffixes.
BASE_URL="${BASE_URL%/}"

CURL_OPTS=(--silent --show-error --max-time 15 --connect-timeout 5)

failed=0

# ── Helpers ─────────────────────────────────────────────────────────────────

# check_status <name> <expected> <method> <path> [data] [extra_header]
# Prints a one-line PASS/FAIL summary; sets `failed=1` on mismatch.
# Captures HTTP status + small body excerpt for the failure log.
check_status() {
  local name="$1" expected="$2" method="$3" path="$4" data="${5:-}" header="${6:-}"
  local url="${BASE_URL}${path}"
  local body_file status

  body_file="$(mktemp)"
  if [[ "$method" == "GET" ]]; then
    if [[ -n "$header" ]]; then
      status="$(curl "${CURL_OPTS[@]}" -o "$body_file" -w '%{http_code}' \
        -H "$header" "$url" || echo "000")"
    else
      status="$(curl "${CURL_OPTS[@]}" -o "$body_file" -w '%{http_code}' "$url" || echo "000")"
    fi
  else
    status="$(curl "${CURL_OPTS[@]}" -o "$body_file" -w '%{http_code}' \
      -X "$method" -H "Content-Type: application/json" --data "$data" "$url" || echo "000")"
  fi

  if [[ "$status" == "$expected" ]]; then
    echo "✓ ${name} (${status})"
    rm -f "$body_file"
    return 0
  fi

  echo "✗ ${name}: expected ${expected}, got ${status}"
  echo "  body: $(head -c 200 "$body_file" | tr -d '\n')"
  rm -f "$body_file"
  failed=1
  return 1
}

# Mask a string: keep first 4 + last 4 chars, replace middle with stars.
# Used to print a token-presence proof without exposing the token itself.
mask() {
  local s="$1"
  local len=${#s}
  if (( len < 12 )); then
    echo "[redacted ${len}b]"
  else
    echo "${s:0:4}…${s: -4} (${len}b)"
  fi
}

# ── Checks ──────────────────────────────────────────────────────────────────

echo "Smoke testing ${BASE_URL}"
echo

check_status "GET /health"  200 GET "/health"  || true
check_status "GET /ready"   200 GET "/ready"   || true

# Login. Verifies three things end-to-end:
#   (a) JWT issuance: 200 with a non-empty access_token in the body.
#   (b) Cookie write-path: Set-Cookie: refresh_token=... in response
#       headers. This is exactly the FastAPI cookie merge gotcha that
#       broke SSO in PR #78 — a passing /login that silently fails to
#       set the refresh cookie still looks green from the body alone.
#   (c) MFA invariant: smoke user has MFA disabled.
#
# Build the JSON body via python3's json.dumps so a password containing
# ", \, or a newline doesn't produce invalid JSON. Pass credentials via
# environment so they never appear in argv (visible in `ps`).
login_body="$(SMOKE_USER="$USERNAME" SMOKE_PWD="$PASSWORD" python3 -c '
import json, os
print(json.dumps({"login": os.environ["SMOKE_USER"], "password": os.environ["SMOKE_PWD"]}))
')"
login_response="$(mktemp)"
login_headers="$(mktemp)"
login_status="$(curl "${CURL_OPTS[@]}" -o "$login_response" -D "$login_headers" \
  -w '%{http_code}' \
  -X POST -H "Content-Type: application/json" --data "$login_body" \
  "${BASE_URL}/api/v1/auth/login" || echo "000")"

if [[ "$login_status" != "200" ]]; then
  echo "✗ POST /api/v1/auth/login: expected 200, got ${login_status}"
  echo "  body: $(head -c 200 "$login_response" | tr -d '\n')"
  rm -f "$login_response" "$login_headers"
  failed=1
else
  # Extract the access_token without leaking it. python3 is preinstalled
  # on ubuntu-latest, so a small json.load is the safest extractor.
  access_token="$(python3 -c '
import json, sys
data = json.load(open(sys.argv[1]))
if "mfa_required" in data:
    print("MFA_CHALLENGE", file=sys.stderr); sys.exit(1)
print(data.get("access_token", ""))
' "$login_response" 2>&1 || true)"

  # Cookie write-path assertion: refresh_token must be in Set-Cookie.
  # We never print the value — only that the cookie name is present
  # in headers. Header name is case-insensitive per HTTP; the cookie
  # name is case-sensitive per RFC 6265 — and the backend writes
  # exactly `refresh_token` (lowercase). Accepting `Refresh_Token`
  # would mask a real bug, so do NOT use grep -i (it would weaken
  # the PR #78 regression guard). The header-name half uses explicit
  # [Ss][Ee][Tt]-... character classes; the cookie name stays
  # case-sensitive.
  if grep -qE '^[Ss][Ee][Tt]-[Cc][Oo][Oo][Kk][Ii][Ee]:[[:space:]]*refresh_token=' "$login_headers"; then
    cookie_present=1
  else
    cookie_present=0
  fi

  if [[ "$access_token" == *"MFA_CHALLENGE"* ]]; then
    echo "✗ POST /api/v1/auth/login: smoke user has MFA enabled — disable it"
    failed=1
    access_token=""
  elif [[ -z "$access_token" ]]; then
    echo "✗ POST /api/v1/auth/login: 200 but no access_token in body"
    failed=1
  elif (( cookie_present == 0 )); then
    echo "✗ POST /api/v1/auth/login: 200 with token but Set-Cookie: refresh_token=… is missing"
    echo "  headers received: $(grep -ic '^set-cookie:' "$login_headers") Set-Cookie line(s)"
    failed=1
  else
    echo "✓ POST /api/v1/auth/login (200, token=$(mask "$access_token"), refresh_token cookie set)"
  fi
  rm -f "$login_response" "$login_headers"
fi

# Authenticated read. Skip if login failed — no token to use.
if [[ -n "${access_token:-}" ]]; then
  check_status "GET /api/v1/categories (authenticated)" 200 GET \
    "/api/v1/categories" "" "Authorization: Bearer ${access_token}" || true
else
  echo "✗ GET /api/v1/categories: skipped (no access token from login)"
  failed=1
fi

echo

if (( failed == 0 )); then
  echo "All smoke checks passed."
  exit 0
fi

echo "Smoke checks FAILED."
exit 1
