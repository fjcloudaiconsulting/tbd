#!/usr/bin/env bash
# TBD-360 — rehearse the Ubuntu -> Oracle PACKAGE SWAP half of the 8.0 -> 8.4
# cutover, and MEASURE the elapsed data-dictionary upgrade.
#
# WHY THIS EXISTS, given infra/rehearse-84-upgrade.sh already exists. That
# sibling swaps the *container image*, which exercises the engine-level DD
# upgrade — the half Oracle already regression-tests. It cannot touch the half
# where every box-specific failure this runbook enumerates actually lives:
# debian-sys-maint, the /etc/mysql/mysql.conf.d include path, the systemd unit,
# and AppArmor. Those change because the PACKAGE changes, not because the
# binary does. This script performs the real dpkg transaction.
#
# ⚠⚠ THE HEADLINE OUTPUT IS A DURATION. infra/MYSQL-84-CUTOVER.md says the
# maintenance window is "unsized" because nobody has measured the DD upgrade on
# a production-sized datadir. Phase 6 times exactly that, from slow-shutdown to
# the first successful authenticated query on 8.4.
#
# ⚠⚠ THE LOCAL TARGET'S DURATION IS MEANINGLESS AND MUST NEVER BE QUOTED AS
# THE WINDOW SIZE. Synthetic or sampled data, a different disk, and a container
# rather than a droplet. `--target local` validates the HARNESS — that the
# phases run, the swap completes, the assertions fire. The NUMBER only exists
# after `--target droplet` against a snapshot of production. This distinction is
# load-bearing: this repo has already shipped one runbook evidence row that was
# vacuous (a test count from a suite that never touched MySQL).
#
# WHAT THE LOCAL TARGET DOES COVER:
#   * Ubuntu's own mysql-server-8.0 package, installed by apt
#   * the real dpkg transaction replacing it with Oracle mysql-community-server
#   * debian-sys-maint / /etc/mysql/debian.cnf disappearing (phase 9)
#   * whether /etc/mysql/mysql.conf.d is still included by Oracle's packaging,
#     which runbook section 5 gates on (phase 8)
#   * the DD upgrade itself, and every schema assertion
#
# WHAT THE LOCAL TARGET DOES NOT COVER, deliberately and by construction:
#   * the systemd unit — a plain container has no systemd. mysqld is started
#     directly here. The droplet run is the FIRST real test of the unit.
#   * AppArmor — Docker Desktop's Linux VM does not carry the droplet's policy.
#   * DO snapshot / restore / rebuild verbs.
#   * production's RSA keypair for caching_sha2_password over non-TLS.
#   * dataset size, and therefore duration. See above.
#
# Usage:
#   bash infra/rehearse-84-scratch-droplet.sh --target local  [--dump f.sql.gz] [--cfg rendered.cnf] [--keep]
#   bash infra/rehearse-84-scratch-droplet.sh --target droplet --host <ip> --dump /root/pfv2_<date>.sql.gz [--cfg ...]
#
# Exit code is the number of failed gates, so it is non-zero on any failure.
# This gates a one-way door; "read the output carefully" is not good enough.

set -uo pipefail

TARGET=""; HOST=""; DUMP=""; CFG=""; KEEP=0; PROD_DD=""
CTR=tbd360_scratch
UBUNTU_IMAGE=ubuntu:24.04
DB=pfv2
# Pinned deliberately: a floating "latest" would change the rehearsed artefact
# between the rehearsal and the window. Bump it consciously, then re-rehearse.
MYSQL_APT_CONFIG_URL=http://repo.mysql.com/apt/ubuntu/pool/mysql-apt-config/m/mysql-apt-config/mysql-apt-config_0.8.39-1_all.deb

usage() { sed -n '/^# Usage:/,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)  TARGET="${2:-}"; shift 2 ;;
    --host)    HOST="${2:-}";   shift 2 ;;
    --dump)    DUMP="${2:-}";   shift 2 ;;
    --cfg)     CFG="${2:-}";    shift 2 ;;
    --prod-dd) PROD_DD="${2:-}";shift 2 ;;
    --keep)    KEEP=1; shift ;;
    -h|--help) usage ;;
    *) echo "!! unknown argument: $1"; usage ;;
  esac
done

[[ "$TARGET" == "local" || "$TARGET" == "droplet" ]] || usage
[[ "$TARGET" == "droplet" && -z "$HOST" ]] && { echo "!! --target droplet requires --host"; exit 2; }
[[ -n "$DUMP" && ! -f "$DUMP" && "$TARGET" == "local" ]] && { echo "!! no such dump: $DUMP"; exit 2; }

FAILS=0
ok()   { printf "    OK    %s\n" "$*"; }
bad()  { printf "    !! FAIL  %s\n" "$*"; FAILS=$((FAILS+1)); }
note() { printf "    ...   %s\n" "$*"; }
hdr()  { printf "\n=== %s ===\n" "$*"; }

# ---------------------------------------------------------------------------
# The single indirection. Every phase below runs through run()/sql(), so the
# local target executes the SAME code path the droplet does. A separate
# "local test script" would validate something other than what you run.
# ---------------------------------------------------------------------------
# ⚠ The key must be explicit. The DO-registered key is not necessarily the
# one ssh picks by default, and BatchMode then fails with a bare
# "Permission denied (publickey)" that reads like a firewall problem.
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o BatchMode=yes -i "${SSH_KEY:-$HOME/.ssh/id_rsa}")

run() {   # run <shell command string> — stdout/stderr pass through
  if [[ "$TARGET" == local ]]; then docker exec "$CTR" bash -lc "$1"
  else ssh "${SSH_OPTS[@]}" "root@$HOST" bash -lc "$(printf '%q' "$1")"; fi
}
run_in() { # like run(), but pipes this script's stdin through
  if [[ "$TARGET" == local ]]; then docker exec -i "$CTR" bash -lc "$1"
  else ssh "${SSH_OPTS[@]}" "root@$HOST" bash -lc "$(printf '%q' "$1")"; fi
}
# SQL goes over STDIN, never as a quoted argument. Three-layer quote nesting
# through docker/ssh into mysql -e is how you get a query that silently runs
# something other than what you read.
# ⚠⚠ --no-defaults IS LOAD-BEARING. The backups role writes /root/.my.cnf with
# `user = pfv_backup`, so a bare `mysql` run AS ROOT authenticates as
# pfv_backup@localhost -- a deliberately low-privilege account. Every privileged
# step then fails with ERROR 1227 (needs SUPER or SYSTEM_VARIABLES_ADMIN),
# including the `SET GLOBAL innodb_fast_shutdown = 0` that MYSQL-84-CUTOVER.md
# step 4 marks REQUIRED before an in-place upgrade. Measured on a real droplet
# 2026-08-18: it fails, and dpkg then stops mysqld with the DEFAULT fast
# shutdown, starting the DD upgrade from a non-clean state -- the precise
# failure that step exists to prevent, arrived at while appearing to follow it.
# --no-defaults skips /root/.my.cnf, so root@localhost authenticates by socket.
sql()  { run_in "mysql --no-defaults -N -B ${1:-}"; }

cleanup() {
  if [[ "$TARGET" == local && $KEEP -eq 0 ]]; then
    docker rm -f "$CTR" >/dev/null 2>&1
  fi
}
trap cleanup EXIT

echo "TBD-360 scratch rehearsal — target=$TARGET${HOST:+ host=$HOST}"
[[ "$TARGET" == local ]] && echo "⚠ LOCAL: timings are harness validation only, NOT the window size."

# ---------------------------------------------------------------------------
hdr "0. rendered config validates on BOTH engines (it lands on the 8.0 box first)"
if [[ -n "$CFG" && -f "$CFG" ]]; then
  for v in 8.0 8.4; do
    if docker run --rm --entrypoint mysqld -v "$CFG":/etc/mysql/conf.d/pfv.cnf:ro "mysql:$v" \
         --validate-config >/tmp/tbd360_vc_$v.txt 2>&1
    then ok "mysql:$v validate-config exit 0"
    else bad "mysql:$v validate-config REJECTED the config"; head -3 /tmp/tbd360_vc_$v.txt | sed 's/^/          /'; fi
  done
else
  note "no --cfg given; skipping. Pass the RENDERED my.cnf to gate this."
fi

# ---------------------------------------------------------------------------
hdr "1. bring up Ubuntu-packaged MySQL 8.0"
if [[ "$TARGET" == local ]]; then
  docker rm -f "$CTR" >/dev/null 2>&1
  # --init is REQUIRED, not cosmetic. Ubuntu's mysql-server postinst starts a
  # bootstrap mysqld on a temp socket, shuts it down, then polls until the pid
  # disappears. With `sleep infinity` as PID 1 nothing reaps children, so the
  # cleanly-exited mysqld lingers as a zombie, the poll times out, and the
  # postinst dies with "Unable to shut down server with process id N" — leaving
  # dpkg at `iF` (half-configured) and no 8.0 to upgrade FROM. Measured: without
  # --init the install fails 100% of the time; with it, `ii` and exit 0.
  # --platform linux/amd64 is REQUIRED on Apple Silicon. Measured against
  # repo.mysql.com: the Ubuntu repo publishes `Architectures: i386 amd64` only —
  # there is NO arm64. On an arm64 host the swap fails with "Unable to locate
  # package mysql-community-server", which reads like a wrong component name and
  # is not. Under emulation this is slow; correctness beats speed here.
  # ⚠ This also means the production droplet MUST be amd64. DO's s-1vcpu-2gb is,
  # but an ARM droplet would make this entire cutover plan impossible.
  docker run -d --init --platform linux/amd64 --name "$CTR" --cpus=1 -m 2g "$UBUNTU_IMAGE" sleep infinity >/dev/null
  # policy-rc.d 101 stops the postinst trying to start a service in a container
  # with no init. The package still installs and still initialises the datadir,
  # which is what we are here to swap.
  run 'mkdir -p /usr/sbin && printf "#!/bin/sh\nexit 101\n" > /usr/sbin/policy-rc.d && chmod +x /usr/sbin/policy-rc.d' >/dev/null
  note "installing mysql-server-8.0 from Ubuntu (this takes a few minutes)"
  if run 'export DEBIAN_FRONTEND=noninteractive; apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq mysql-server-8.0 curl gnupg ca-certificates > /tmp/install.log 2>&1'; then
    ok "Ubuntu mysql-server-8.0 installed"
  else
    bad "apt install mysql-server-8.0 failed — cannot rehearse"
    run "tail -25 /tmp/install.log" 2>/dev/null | sed 's/^/          /'
    run "dpkg -l | grep mysql-server" 2>/dev/null | sed 's/^/          /'
    exit $FAILS
  fi
  # `iF` means the postinst failed; the datadir may exist but the package is
  # half-configured, and the swap in phase 6 would then be measuring the wrong
  # thing. Gate on it rather than discovering it in phase 6.
  if run "dpkg -l | grep -q '^ii  mysql-server-8.0'" 2>/dev/null; then
    ok "mysql-server-8.0 fully configured (ii)"
  else
    bad "mysql-server-8.0 is not fully configured — check dpkg state above"
  fi
  run '[ -d /var/lib/mysql/mysql ] || mysqld --initialize-insecure --user=mysql' >/dev/null 2>&1
  # ⚠ The config goes in BEFORE mysqld first starts. If it is installed after,
  # phase 4 captures a baseline of 8.0 running on DEFAULTS, and phase 8 then
  # compares "no config" against "config" — reporting bind_address 127.0.0.1 ->
  # 0.0.0.0 and buffer pool 128M -> 768M as include-path failures. Those are the
  # config CORRECTLY applying. Measured: 4 false FAILs before this was moved.
  if [[ -n "$CFG" && -f "$CFG" ]]; then
    docker exec "$CTR" mkdir -p /etc/mysql/mysql.conf.d >/dev/null 2>&1
    docker cp "$CFG" "$CTR":/etc/mysql/mysql.conf.d/zz-pfv.cnf >/dev/null 2>&1 \
      && ok "rendered config installed at /etc/mysql/mysql.conf.d/zz-pfv.cnf" \
      || bad "could not install the rendered config"
  fi
  run 'mkdir -p /var/run/mysqld && chown mysql:mysql /var/run/mysqld; (mysqld --user=mysql --daemonize 2>/dev/null || mysqld_safe --user=mysql >/dev/null 2>&1 &)' >/dev/null 2>&1
else
  note "droplet: assuming the snapshot already carries Ubuntu mysql-server-8.0"
  note "droplet: the config is ansible-managed and already applied; not touching it"
fi

for _ in $(seq 1 40); do echo "SELECT 1" | sql >/dev/null 2>&1 && break; sleep 3; done
V80=$(echo "SELECT VERSION()" | sql 2>/dev/null | head -1)
if [[ -n "$V80" ]]; then ok "8.0 answering: $V80"; else bad "8.0 never became reachable"; exit $FAILS; fi
PKG_BEFORE=$(run "dpkg -l | grep -c mysql-server" 2>/dev/null | tr -d '[:space:]')
note "mysql-server packages installed: ${PKG_BEFORE:-?}"

# ---------------------------------------------------------------------------
hdr "2. load the dataset"
if [[ -n "$DUMP" ]]; then
  echo "CREATE DATABASE IF NOT EXISTS $DB CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;" | sql
  # ⚠ The nightly artifact is dumped WITHOUT --databases, so it carries no
  # CREATE DATABASE and no USE. It MUST be restored into a named database or it
  # dies with ERROR 1046. See MYSQL-84-CUTOVER.md section 2.
  if [[ "$TARGET" == local ]]; then
    if [[ "$DUMP" == *.gz ]]; then gzcat "$DUMP" 2>/dev/null || zcat "$DUMP"; else cat "$DUMP"; fi \
      | docker exec -i "$CTR" bash -lc "mysql --no-defaults $DB" && ok "dump restored into $DB" || bad "dump restore failed"
  else
    run "set -o pipefail; zcat $DUMP | mysql --no-defaults $DB" && ok "dump restored into $DB" || bad "dump restore failed"
  fi
else
  note "no --dump; loading the synthetic constructs a DD upgrade re-parses"
  run_in "mysql --no-defaults" <<SYNTH
CREATE DATABASE IF NOT EXISTS $DB CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE $DB;
CREATE TABLE organizations (
  id INT PRIMARY KEY AUTO_INCREMENT,
  name VARCHAR(200) NOT NULL,
  settings JSON NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
ALTER TABLE organizations
  ADD COLUMN name_normalized VARCHAR(200)
  GENERATED ALWAYS AS (LOWER(name)) STORED COLLATE utf8mb4_0900_as_cs;
ALTER TABLE organizations ADD CONSTRAINT uq_org_name_norm UNIQUE (name_normalized);
CREATE TABLE transactions (
  id INT PRIMARY KEY AUTO_INCREMENT,
  org_id INT NOT NULL,
  linked_transaction_id INT NULL,
  amount DECIMAL(12,2) NOT NULL,
  status ENUM('pending','settled','rejected') NOT NULL DEFAULT 'pending',
  settled_date DATE NULL,
  payload JSON NULL,
  CONSTRAINT fk_tx_org FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE,
  CONSTRAINT fk_tx_link FOREIGN KEY (linked_transaction_id) REFERENCES transactions(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
ALTER TABLE transactions ADD CONSTRAINT ck_tx_settled_implies_date
  CHECK (status <> 'settled' OR settled_date IS NOT NULL);
INSERT INTO organizations (name, settings) VALUES ('Acme Household', '{"currency":"EUR"}');
INSERT INTO transactions (org_id, amount, status, settled_date, payload)
  VALUES (1, 6500.00, 'settled', '2026-03-25', '{"source":"salary"}'), (1, 42.55, 'pending', NULL, NULL);
SYNTH
  ok "synthetic constructs loaded"
fi

# ---------------------------------------------------------------------------
hdr "3. data-dictionary version, vs production"
DD=$(echo "SELECT properties FROM mysql.dd_properties" | sql 2>/dev/null | head -c 200)
note "dd_properties: $DD"
DDNUM=$(printf '%s' "$DD" | grep -oE 'DD_VERSION=[0-9]+' | head -1 | cut -d= -f2)
if [[ -n "$PROD_DD" ]]; then
  if [[ "$DDNUM" == "$PROD_DD" ]]; then ok "DD_VERSION $DDNUM matches production"
  else bad "DD_VERSION $DDNUM != production's $PROD_DD — this is NOT a faithful rehearsal"; fi
else
  note "DD_VERSION=$DDNUM (pass --prod-dd to gate this against production)"
fi

# ---------------------------------------------------------------------------
hdr "4. capture the 8.0 baseline"
BASE=$(run_in "mysql --no-defaults -N -B" <<BASELINE
SELECT CONCAT('tables=',(SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$DB' AND table_type='BASE TABLE'),
' gen=',(SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='$DB' AND extra LIKE '%GENERATED%'),
' chk=',(SELECT COUNT(*) FROM information_schema.table_constraints WHERE constraint_schema='$DB' AND constraint_type='CHECK'),
' fk=',(SELECT COUNT(*) FROM information_schema.table_constraints WHERE constraint_schema='$DB' AND constraint_type='FOREIGN KEY'));
BASELINE
)
echo "    8.0: $BASE"
echo "SHOW GLOBAL VARIABLES" | sql 2>/dev/null | sort > /tmp/tbd360_vars_80.txt
note "captured $(wc -l < /tmp/tbd360_vars_80.txt | tr -d ' ') variables"
if run "test -f /etc/mysql/debian.cnf" 2>/dev/null; then
  ok "/etc/mysql/debian.cnf present on 8.0 (Ubuntu's logrotate authenticates with it)"
  DEBIAN_CNF_BEFORE=1
else
  note "/etc/mysql/debian.cnf absent even on 8.0"; DEBIAN_CNF_BEFORE=0
fi

# ---------------------------------------------------------------------------
hdr "5. slow shutdown — REQUIRED before an in-place upgrade"
echo "SET GLOBAL innodb_fast_shutdown = 0;" | sql 2>/dev/null && ok "innodb_fast_shutdown = 0" || bad "could not set innodb_fast_shutdown"
run "mysqladmin --no-defaults shutdown" >/dev/null 2>&1
for _ in $(seq 1 40); do echo "SELECT 1" | sql >/dev/null 2>&1 || break; sleep 2; done
if echo "SELECT 1" | sql >/dev/null 2>&1; then bad "8.0 did not shut down"; else ok "clean shutdown complete"; fi

# ---------------------------------------------------------------------------
hdr "6. ⏱  PACKAGE SWAP + DD UPGRADE — this is the measured window"
T0=$(date +%s)
# Non-interactive equivalent of what mysql-apt-config writes. mysql-apt-config
# is a debconf wizard; scripting it means answering a TUI. The artefact it
# produces is exactly this repo line, so this is the same transaction.
CODENAME=$(run "( . /etc/os-release; echo \$VERSION_CODENAME )" 2>/dev/null | tr -d '[:space:]')
note "distro codename: ${CODENAME:-unknown}"
# Oracle's RECOMMENDED path: the mysql-apt-config release package, driven
# non-interactively via debconf. The runbook's section 4 says to use it, and
# this script previously hand-rolled the sources.list + key instead, for
# scripting convenience. That deviation was wrong twice over: it made the
# rehearsal LESS faithful than the thing it rehearses, and it manufactured a
# false finding (see below).
#
# ⚠ DO NOT hand-roll the signing key. The standalone RPM-GPG-KEY-mysql-2023
# file on repo.mysql.com EXPIRED 2025-10-22, and apt then rejects the repo with
# `EXPKEYSIG B7B3B788A8D3785C`, surfacing as the very misleading "E: Unable to
# locate package mysql-community-server". mysql-apt-config embeds the RENEWED
# key (same id, expires 2027-10-23) in its postinst and installs it to
# /usr/share/keyrings/mysql-apt-config.gpg with signed-by=. Measured 2026-08-18
# against mysql-apt-config 0.8.39-1: valid key, repo usable, candidate 8.4.11.
# ⚠ dev.mysql.com's quick guide still documents `apt-key adv --recv-keys
# A8D3785C`, which can resolve the STALE key from a keyserver. Use the package.
# ⚠ mysql-apt-config declares Pre-Depends: debconf, dpkg, lsb-release, wget,
# bash, gnupg. Pre-Depends are not auto-resolved by `dpkg -i`, so a missing one
# aborts the install outright — and the message names only the FIRST missing
# package, so discovering them one run at a time costs a full rehearsal each.
# Installed from the package's own declared list, not from guesswork.
run "export DEBIAN_FRONTEND=noninteractive; apt-get install -y -qq lsb-release wget gnupg debconf debconf-utils" >/dev/null 2>&1
run "curl -fsSL -o /tmp/mysql-apt-config.deb $MYSQL_APT_CONFIG_URL" >/dev/null 2>&1 \
  && ok "fetched mysql-apt-config" || bad "could not fetch mysql-apt-config"
run_in "debconf-set-selections" <<PRESEED
mysql-apt-config mysql-apt-config/repo-distro select ubuntu
mysql-apt-config mysql-apt-config/repo-codename select $CODENAME
mysql-apt-config mysql-apt-config/repo-url string http://repo.mysql.com/apt
mysql-apt-config mysql-apt-config/select-server select mysql-8.4-lts
mysql-apt-config mysql-apt-config/select-connectors select Disabled
mysql-apt-config mysql-apt-config/select-product select Ok
PRESEED
if run "export DEBIAN_FRONTEND=noninteractive; dpkg -i /tmp/mysql-apt-config.deb > /tmp/aptcfg.log 2>&1"; then
  ok "mysql-apt-config installed and configured for mysql-8.4-lts"
else
  bad "dpkg -i mysql-apt-config failed"
  run "tail -12 /tmp/aptcfg.log" 2>/dev/null | sed 's/^/          /'
fi
KEYEXP=$(run "gpg --show-keys /usr/share/keyrings/mysql-apt-config.gpg 2>/dev/null | grep -oE 'expires: [0-9-]+'" 2>/dev/null | head -1)
if [[ -n "$KEYEXP" ]]; then ok "signing key installed by the package (${KEYEXP})"
else bad "no signing key at /usr/share/keyrings/mysql-apt-config.gpg"; fi
run "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq" >/dev/null 2>&1
# Gate on a resolvable CANDIDATE, not on apt-get update's exit code. update
# returns 0 with an unusable repo when signature verification fails, so its exit
# status is not evidence the package can be installed. This gate is what caught
# the expired-key failure above.
CAND=$(run "apt-cache policy mysql-community-server 2>/dev/null | awk '/Candidate:/{print \$2}'" 2>/dev/null | tr -d '[:space:]')
if [[ -n "$CAND" && "$CAND" != "(none)" ]]; then ok "Oracle 8.4 repo usable — candidate $CAND"
else bad "no installable mysql-community-server candidate (expired key? wrong arch? wrong codename?)"; fi

note "replacing Ubuntu mysql-server-8.0 with Oracle mysql-community-server"
if run 'export DEBIAN_FRONTEND=noninteractive; apt-get install -y -qq -o Dpkg::Options::=--force-confold mysql-community-server > /tmp/swap.log 2>&1'; then
  ok "package swap completed"
else
  bad "PACKAGE SWAP FAILED — this is the whole point of the rehearsal"
  run "tail -25 /tmp/swap.log" 2>/dev/null | sed 's/^/          /'
fi

if [[ "$TARGET" == local ]]; then
  run 'mkdir -p /var/run/mysqld && chown mysql:mysql /var/run/mysqld; (mysqld --user=mysql --daemonize 2>/dev/null || mysqld_safe --user=mysql >/dev/null 2>&1 &)' >/dev/null 2>&1
else
  run "systemctl start mysql || systemctl start mysqld" >/dev/null 2>&1
fi

# ⚠ The gate is "8.4 answers", NOT "something answers". If the swap failed, the
# OLD 8.4-less server restarts and answers SELECT 1 immediately — which reported
# "OK 8.4 answering after the in-place DD upgrade" while still on 8.0.46, and
# put a 2-second "upgrade time" in the verdict. A gate that passes for the wrong
# reason is worse than no gate: this one would have certified the window size.
UP=0; VER=""
for _ in $(seq 1 100); do
  VER=$(echo "SELECT VERSION()" | sql 2>/dev/null | head -1)
  if [[ "$VER" == 8.4* ]]; then UP=1; break; fi
  sleep 3
done
T1=$(date +%s)
ELAPSED=$((T1-T0))
if [[ $UP -eq 1 ]]; then
  ok "8.4 answering after the in-place DD upgrade ($VER)"
else
  bad "NOT ON 8.4 after the swap — server reports '${VER:-nothing}'"
  note "the elapsed time below is therefore MEANINGLESS; the upgrade did not happen"
fi

# ---------------------------------------------------------------------------
hdr "7. did the risky constructs survive?"
V84=$(echo "SELECT VERSION()" | sql 2>/dev/null | head -1)
if [[ "$V84" == 8.4* ]]; then ok "running $V84"; else bad "expected 8.4, got '${V84:-nothing}'"; fi
AFTER=$(run_in "mysql --no-defaults -N -B" <<AFTERSQL
SELECT CONCAT('tables=',(SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$DB' AND table_type='BASE TABLE'),
' gen=',(SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='$DB' AND extra LIKE '%GENERATED%'),
' chk=',(SELECT COUNT(*) FROM information_schema.table_constraints WHERE constraint_schema='$DB' AND constraint_type='CHECK'),
' fk=',(SELECT COUNT(*) FROM information_schema.table_constraints WHERE constraint_schema='$DB' AND constraint_type='FOREIGN KEY'));
AFTERSQL
)
echo "    8.4: $AFTER"
if [[ -n "$AFTER" && "$AFTER" == "$BASE" ]]; then ok "schema inventory identical to the 8.0 baseline"
else bad "INVENTORY MISMATCH — 8.0 was: $BASE"; fi

# Enforcement, not DDL presence. A CHECK can appear in SHOW CREATE TABLE and
# still not be enforced; only a rejected INSERT proves it.
if [[ -z "$DUMP" ]]; then
  echo "    CHECK still ENFORCED (this INSERT must be rejected):"
  if run_in "mysql --no-defaults $DB" <<<"INSERT INTO transactions (org_id,amount,status,settled_date) VALUES (1,1.00,'settled',NULL);" >/dev/null 2>&1
  then bad "CHECK NOT ENFORCED — the constraint did not survive"; else ok "rejected"; fi
  echo "    UNIQUE on the generated column still ENFORCED:"
  if run_in "mysql --no-defaults $DB" <<<"INSERT INTO organizations (name) VALUES ('ACME HOUSEHOLD');" >/dev/null 2>&1
  then bad "UNIQUE NOT ENFORCED"; else ok "rejected"; fi
  echo "    generated-column collation:"
  run_in "mysql --no-defaults -N -B" <<<"SELECT CONCAT('      ',column_name,' ',collation_name) FROM information_schema.columns WHERE table_schema='$DB' AND extra LIKE '%GENERATED%';" 2>/dev/null
else
  note "restored a real dump; skipping the synthetic enforcement probes"
  note "assert by hand: SHOW CREATE TABLE organizations (generated col, utf8mb4_0900_as_cs)"
  note "assert by hand: SHOW CREATE TABLE transactions (named CHECK)"
fi

# ---------------------------------------------------------------------------
hdr "8. config include path — runbook section 5's gate, asserted BY NAME"
# Only a HARD gate when a config was actually deployed. With no config there is
# nothing to include, so a difference is 8.4 re-defaulting, not a dropped
# include — reporting that as FAIL would be a fence certifying its own gap.
GATED=0
if [[ -n "$CFG" && -f "$CFG" ]]; then GATED=1; else
  note "no --cfg in play: differences below are 8.4 re-defaults, reported not gated"
fi
# `mysqld --validate-config` CANNOT catch this: if the file is not included it
# is not read, and validation still exits 0. Only reading the live values does.
echo "SHOW GLOBAL VARIABLES" | sql 2>/dev/null | sort > /tmp/tbd360_vars_84.txt
for v in bind_address collation_server innodb_buffer_pool_size innodb_io_capacity innodb_io_capacity_max innodb_redo_log_capacity; do
  B=$(grep -E "^${v}[[:space:]]" /tmp/tbd360_vars_80.txt | cut -f2-)
  A=$(grep -E "^${v}[[:space:]]" /tmp/tbd360_vars_84.txt | cut -f2-)
  if [[ -z "$A" ]]; then bad "$v absent on 8.4"
  elif [[ "$B" == "$A" ]]; then ok "$(printf '%-28s %s' "$v" "$A")"
  elif [[ $GATED -eq 1 ]]; then bad "$(printf '%-28s %s -> %s' "$v" "$B" "$A")"
  else note "$(printf '%-28s %s -> %s  (no config in play; 8.4 re-default)' "$v" "$B" "$A")"; fi
done
note "innodb_redo_log_capacity must be 268435456; 100M means the include was dropped"

# ---------------------------------------------------------------------------
hdr "9. debian-sys-maint / debian.cnf after the swap"
# Ubuntu's logrotate authenticates with this account. Oracle's packaging does
# not create it, so the slow query log silently stops rotating and fills the
# disk DAYS later. Silent by construction, which is why it is a named gate.
if run "test -f /etc/mysql/debian.cnf" 2>/dev/null; then
  note "/etc/mysql/debian.cnf still present"
else
  if [[ "$DEBIAN_CNF_BEFORE" -eq 1 ]]; then
    bad "/etc/mysql/debian.cnf GONE after the swap — logrotate will fail silently"
  else
    note "/etc/mysql/debian.cnf absent (was already absent on 8.0)"
  fi
fi
DSM=$(run_in "mysql --no-defaults -N -B" <<<"SELECT COUNT(*) FROM mysql.user WHERE user='debian-sys-maint';" 2>/dev/null | tr -d '[:space:]')
note "debian-sys-maint accounts in mysql.user: ${DSM:-?}"
echo "    accounts NOT on caching_sha2_password (expect only root@localhost/auth_socket):"
run_in "mysql --no-defaults -N -B" <<<"SELECT CONCAT('      ',user,'@',host,' ',plugin) FROM mysql.user WHERE plugin <> 'caching_sha2_password';" 2>/dev/null

# ---------------------------------------------------------------------------
hdr "10. verdict"
if [[ $UP -eq 1 ]]; then
  printf "    elapsed, slow-shutdown -> first authenticated query on 8.4: %dm %02ds\n" $((ELAPSED/60)) $((ELAPSED%60))
else
  echo "    NO DURATION: the server never reached 8.4, so nothing was measured."
fi
if [[ "$TARGET" == local ]]; then
  echo "    ⚠ LOCAL RUN — this duration validates the harness ONLY."
  echo "      It is NOT the maintenance window. Re-run with --target droplet"
  echo "      against a snapshot of production to size the window."
else
  echo "    ^ THIS is the number infra/MYSQL-84-CUTOVER.md sizes the window from."
  echo "      Add the cold snapshot on top. (An App Platform scale-down/up was"
  echo "      also assumed here, but it is NOT available on this plan - TBD-416."
  echo "      The executed 2026-08-19 window ran ~24 min, ~8 min DB-down.)"
fi
echo
if [[ $FAILS -eq 0 ]]; then
  echo "    ALL GATES PASSED ($TARGET)"
else
  echo "    $FAILS GATE(S) FAILED — do not schedule the window on this result"
fi
[[ "$TARGET" == local && $KEEP -eq 1 ]] && echo "    container kept: docker exec -it $CTR bash"
exit $FAILS
