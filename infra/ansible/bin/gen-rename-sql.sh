#!/usr/bin/env bash
# TBD-360 Phase 2 — emit the pfv2 -> tbd rename as ONE atomic statement.
#
# WHY GENERATED, NOT HAND-TYPED. There are 83 FK declarations across the models.
# `RENAME TABLE` executed per-table would leave foreign keys pointing at tables
# not yet moved and fail partway, with the schema half-renamed. MySQL renames
# every pair atomically inside a single statement -- so the statement must list
# ALL tables, and a truncated list is a half-rename that looks like a success.
# Hence: read the table list from information_schema, and ASSERT the emitted
# pair count matches what was read before printing anything.
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
# everything we need. See MYSQL-84-CUTOVER.md.
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
  echo "CREATE DATABASE IF NOT EXISTS \`$TO\` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
  echo "RENAME TABLE"
  printf '%s\n' "$TABLES" | grep . | awk -v f="$FROM" -v t="$TO" '
    {rows[NR]=sprintf("  %s.`%s` TO %s.`%s`", f, $0, t, $0)}
    END {for(i=1;i<=NR;i++) printf "%s%s\n", rows[i], (i<NR ? "," : ";")}'
} > "$OUT"

# The assertion the spec demands: emitted pairs must equal tables read.
EMITTED=$(grep -c ' TO ' "$OUT")
if [[ "$EMITTED" -ne "$COUNT" ]]; then
  echo "!! TRUNCATED: read $COUNT tables but emitted $EMITTED pairs. Refusing." >&2
  exit 1
fi
cat "$OUT"
echo "-- asserted: $EMITTED pairs == $COUNT tables read from information_schema" 
