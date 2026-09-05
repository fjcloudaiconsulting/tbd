#!/usr/bin/env bash
# TBD-360 Phase 2 — emit the pfv2 -> tbd rename as ONE atomic statement.
#
# WHY GENERATED, NOT HAND-TYPED. There are 83 FK declarations across the models.
#
# ⚠ The reason is NOT that a per-table rename would "fail partway" -- this file
# claimed that for months and it is FALSE. Measured on MySQL 8.4.11, production's
# exact version, 2026-08-27 (TBD-416): renaming a parent out from under its child
# SUCCEEDS, and MySQL silently REWRITES the child's foreign key to point across
# schemas. The probe left `src.child` holding a live, ENFORCED cross-schema FK to
# `dst.parent`: a bad reference still raised errno 1452, a good one still
# inserted. So a truncated rename does not announce itself. It yields a working
# database wired across two schemas, which breaks later and elsewhere -- most
# obviously the moment the old schema is dropped.
#
# Silent success is strictly worse than failure, and it is the whole reason the
# pair count is ASSERTED rather than eyeballed. MySQL renames every pair
# atomically inside a single statement -- so the statement must list ALL tables,
# and a truncated list is a half-rename that looks like a success. Hence: read
# the table list from information_schema, and ASSERT the emitted pair count
# matches what was read before printing anything.
#
# Metadata-only for InnoDB: no data copy, fast regardless of table size.
# ⚠ Views, triggers and stored routines do NOT move with RENAME TABLE. This
# script refuses to run if any exist, because their presence would make the
# rename silently partial. (Verified 2026-08-18: production has none.)
#
# Usage:
#   bin/gen-rename-sql.sh --host <ip> [--from pfv2] [--to tbd] > rename.sql
set -euo pipefail

HOST=""; FROM="pfv2"; TO="tbd"; SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="${2:-}"; shift 2 ;;
    --from) FROM="${2:-}"; shift 2 ;;
    --to)   TO="${2:-}";   shift 2 ;;
    *) echo "!! unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$HOST" ]] || { echo "!! --host is required" >&2; exit 2; }

remote() { ssh -o BatchMode=yes -o ConnectTimeout=10 -i "$SSH_KEY" "root@$HOST" "$1"; }

# --no-defaults: /root/.my.cnf forces user=pfv_backup, which cannot see
# everything we need.
OBJS=$(remote "mysql --no-defaults -N -B -e \"SELECT CONCAT(
  (SELECT COUNT(*) FROM information_schema.views    WHERE table_schema='$FROM'),'/',
  (SELECT COUNT(*) FROM information_schema.triggers WHERE trigger_schema='$FROM'),'/',
  (SELECT COUNT(*) FROM information_schema.routines WHERE routine_schema='$FROM'))\"")
if [[ "$OBJS" != "0/0/0" ]]; then
  echo "!! $FROM has views/triggers/routines ($OBJS as views/triggers/routines)." >&2
  echo "   RENAME TABLE does not move them, so this would be a PARTIAL rename." >&2
  exit 1
fi

TABLES=$(remote "mysql --no-defaults -N -B -e \"SELECT table_name FROM information_schema.tables WHERE table_schema='$FROM' AND table_type='BASE TABLE' ORDER BY table_name\"")
COUNT=$(printf '%s\n' "$TABLES" | grep -c . || true)
[[ "$COUNT" -gt 0 ]] || { echo "!! no base tables found in $FROM" >&2; exit 1; }

OUT=$(mktemp); trap 'rm -f "$OUT"' EXIT
{
  echo "-- TBD-360 Phase 2: $FROM -> $TO. Generated $(date -u +%Y-%m-%dT%H:%M:%SZ)."
  echo "-- $COUNT base tables, renamed in ONE atomic statement."
  # TBD-416. This is the quiesce. The app cannot be scaled to zero (see
  # infra/MIGRATION.md step 2), so the rename runs against a live backend and
  # MUST be able to give up rather than park behind a metadata lock forever --
  # the server default is 365 days.
  #
  # ⚠ It is emitted HERE, as the first executable line, because the runbook pipes
  # this whole file into ONE session (`mysql --no-defaults < rename.sql`). A SET
  # issued as a separate `mysql -e` is a DIFFERENT session and evaporates before
  # the RENAME ever runs, while reading in the runbook as though it did something.
  #
  # ⚠ 10 is deliberately BELOW the server-wide 30 pinned in my.cnf.j2. The global
  # is also the victim's timeout, so session < global means the RENAME yields
  # first and the queue behind it drains with no user-visible errors. Inverting
  # them errors real users while the rename keeps waiting. Metadata-only and
  # atomic, so a timeout costs nothing: fix the blocker and run it again.
  echo "SET SESSION lock_wait_timeout = 10;"
  echo "CREATE DATABASE IF NOT EXISTS \`$TO\` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
  echo "RENAME TABLE"
  printf '%s\n' "$TABLES" | grep . | awk -v f="$FROM" -v t="$TO" '
    {rows[NR]=sprintf("  %s.`%s` TO %s.`%s`", f, $0, t, $0)}
    END {for(i=1;i<=NR;i++) printf "%s%s\n", rows[i], (i<NR ? "," : ";")}'
} > "$OUT"

# TBD-416: the session bound is load-bearing and silent when absent -- without
# it the RENAME inherits the 365-day default and hangs instead of failing. Assert
# it reached the artifact, and assert it precedes the RENAME, since a SET issued
# after the statement it is meant to bound does nothing.
# ⚠ The `|| true` is load-bearing, not defensive noise. Under this script's
# `set -euo pipefail` a non-matching grep exits 1, pipefail propagates it to the
# pipeline, the assignment inherits that status and `set -e` kills the script
# HERE -- before the `if` below can run. Measured: the script exited 1 having
# printed NOTHING, so both diagnostics were unreachable and the operator got a
# bare non-zero exit mid-window. It still failed closed, which is exactly why
# nobody would have noticed.
SET_LINE=$(grep -n '^SET SESSION lock_wait_timeout' "$OUT" | head -1 | cut -d: -f1 || true)
RENAME_LINE=$(grep -n '^RENAME TABLE' "$OUT" | head -1 | cut -d: -f1 || true)
if [[ -z "$SET_LINE" || -z "$RENAME_LINE" || "$SET_LINE" -ge "$RENAME_LINE" ]]; then
  echo "!! session lock_wait_timeout bound missing or not before RENAME TABLE." >&2
  echo "   Refusing: the rename would inherit the 365-day server default." >&2
  exit 1
fi

# The assertion the spec demands: emitted pairs must equal tables read.
EMITTED=$(grep -c ' TO ' "$OUT")
if [[ "$EMITTED" -ne "$COUNT" ]]; then
  echo "!! TRUNCATED: read $COUNT tables but emitted $EMITTED pairs. Refusing." >&2
  exit 1
fi
cat "$OUT"
echo "-- asserted: $EMITTED pairs == $COUNT tables read from information_schema" 
