#!/usr/bin/env bash
# Validate a nightly MySQL backup pair BEFORE it is published or uploaded.
#
# Usage: mysql-backup-verify.sh <dump.sql.gz> <grants.sql.gz> <expected_tables>
#
# Exit 0 = both artifacts are complete and structurally sound.
# Exit 1 = an artifact is bad. The caller must NOT publish or upload it.
# Exit 2 = the check could not run (bad arguments, missing file).
#
# ⚠⚠ THIS IS A PLAIN FILE, NOT A .j2 TEMPLATE, AND THAT IS DELIBERATE.
# Verification logic embedded in a Jinja template can only ever be grep-fenced,
# and in this repo a grep is routinely satisfied by a comment. As a real file it
# can be EXECUTED by the test suite against fabricated good and bad artifacts,
# so every branch below is proven to fire rather than merely proven to exist.
#
# WHY EACH CHECK EXISTS. The three are not interchangeable; each catches a
# failure the others pass. Measured on the real pipeline shape 2026-08-27:
#
#   producer HANGS  -> gzip never sees EOF, never writes the CRC32/ISIZE
#       trailer. `gzip -t` FAILS. Check 1 catches it; checks 2 and 3 never run.
#   producer EXITS NONZERO mid-stream (e.g. errno 1205 under the lock_wait
#       bound) -> gzip sees EOF and finalizes, so `gzip -t` PASSES on a
#       truncated file with a plausible size. ⚠ Check 1 CANNOT see this.
#       Check 2 is the only thing that does.
#   producer completes against a PARTIAL SCHEMA -> the file is whole and
#       carries the completion marker, so checks 1 and 2 both pass. Only the
#       table count sees it.
#
# ⚠ `set -o pipefail` makes `zcat BIG | grep -q PATTERN` fail on a GOOD file:
# grep exits at the first match, zcat takes SIGPIPE, pipefail propagates the
# failure. Every check below therefore reads to EOF (grep -c, compared
# numerically) or lands its input in a variable first. Do not "simplify" these
# to `grep -q`; it inverts the result on exactly the healthy case.
set -euo pipefail

die()  { echo "!! $*" >&2; exit 1; }
usage(){ echo "usage: $0 <dump.sql.gz> <grants.sql.gz> <expected_tables>" >&2; exit 2; }

[[ $# -eq 3 ]] || usage
DUMP="$1"; GRANTS="$2"; EXPECTED_TABLES="$3"

[[ -f "$DUMP"   ]] || { echo "!! dump not found: $DUMP" >&2; exit 2; }
[[ -f "$GRANTS" ]] || { echo "!! grants not found: $GRANTS" >&2; exit 2; }
[[ "$EXPECTED_TABLES" =~ ^[0-9]+$ ]] || { echo "!! expected_tables must be a number, got: $EXPECTED_TABLES" >&2; exit 2; }
[[ "$EXPECTED_TABLES" -gt 0 ]] || { echo "!! expected_tables must be > 0" >&2; exit 2; }

# --- 1. Both artifacts are structurally valid gzip -------------------------
gzip -t "$DUMP"   2>/dev/null || die "dump is not a valid gzip stream (truncated writer? disk full?): $DUMP"
gzip -t "$GRANTS" 2>/dev/null || die "grants file is not a valid gzip stream: $GRANTS"

# --- 2. The dump reached its end ------------------------------------------
# mysqldump writes `-- Dump completed on <date>` as its final line. Its ABSENCE
# is the only in-band evidence that a structurally valid gzip is nonetheless a
# partial dump.
tail_lines="$(gzip -dc "$DUMP" | tail -2)"
if [[ "$(printf '%s\n' "$tail_lines" | grep -c '^-- Dump completed on ')" -eq 0 ]]; then
  die "dump has no '-- Dump completed on' marker: it is a TRUNCATED dump that gzip -t accepts. Last lines were: ${tail_lines//$'\n'/ | }"
fi

# --- 3. The dump covers the whole schema ----------------------------------
# ⚠ expected_tables is passed in, read live from information_schema by the
# caller. It is NOT hardcoded: the count is a measurement of today's schema,
# not an invariant, and a literal here would turn the next migration into a red
# check against a perfectly good backup.
# `|| true` because grep -c exits 1 on zero matches, which set -e would treat
# as a crash rather than as the finding it is.
found_tables="$(gzip -dc "$DUMP" | grep -c '^CREATE TABLE ' || true)"
[[ "$found_tables" -eq "$EXPECTED_TABLES" ]] || \
  die "dump contains $found_tables CREATE TABLE statements, expected $EXPECTED_TABLES. The dump is complete but covers a partial schema."

# --- 4. The grants artifact can actually restore the app account ----------
# A grants file that omits pfv_app yields a restore with tables and zero
# logins, which is the exact hole this artifact exists to close (and a TBD-360
# rollback dependency).
# ⚠ MySQL quotes identifiers with BACKTICKS in SHOW CREATE USER output:
#
#   CREATE USER `pfv_app`@`%` IDENTIFIED WITH 'caching_sha2_password' AS '...'
#
# The first version of this check looked for 'pfv_app' in SINGLE quotes and
# therefore never matched, refusing every backup on a grants file that was
# perfectly correct. Measured against production 8.4.11 on 2026-08-28.
#
# Normalise the quoting and then match ONE canonical form, rather than writing
# an alternation that is easy to get subtly wrong a second time. Under
# ANSI_QUOTES the server would emit double quotes, so both are folded.
found_app="$(gzip -dc "$GRANTS" | tr '`"' "''" | grep -c "^CREATE USER 'pfv_app'@" || true)"
[[ "$found_app" -ge 1 ]] || \
  die "grants file has no CREATE USER for 'pfv_app': a restore from it would produce tables and zero logins."

echo "ok: dump=$found_tables tables, completion marker present, grants include pfv_app"
