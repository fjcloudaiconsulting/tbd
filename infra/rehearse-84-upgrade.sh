#!/usr/bin/env bash
# TBD-360 — rehearse the in-place MySQL 8.0 -> 8.4 data-dictionary upgrade.
#
# WHY THIS EXISTS AS A SCRIPT: the runbook's evidence table has already shipped
# one vacuous row (a test count from a suite that never touched MySQL). Prose
# about a container that no longer exists is not reproducible evidence. Run
# this, read the output, and the claims in infra/MYSQL-84-CUTOVER.md are
# auditable rather than arguable.
#
# WHAT IT DOES rehearse: the engine-level in-place data-dictionary upgrade,
# against the schema constructs that upgrade actually re-parses -- a STORED
# generated column with a non-default collation, a named CHECK constraint with
# an expression, JSON columns, and a self-referencing FK. Those are the classes
# where a DD upgrade aborts or leaves a table unusable. A plain table of
# INT/VARCHAR/ENUM columns is the class LEAST likely to fail and proves little.
#
# WHAT IT DOES NOT rehearse, and no container can:
#   * the Ubuntu `mysql-server-8.0` -> Oracle `mysql-community-server` PACKAGE
#     swap, which is where debian-sys-maint, AppArmor, the systemd unit and the
#     /etc/mysql/mysql.conf.d include path change;
#   * a restore of the real production dataset (row counts, table count, and
#     therefore upgrade DURATION);
#   * production's RSA keypair for caching_sha2_password over non-TLS -- the
#     container generates its own at initialize.
# The scratch-droplet rehearsal remains outstanding and is still the
# highest-value remaining pre-flight.
#
# ⚠ Runs the servers under `--cpus=1 --cpuset-cpus=0 -m 2g` to match pfv-data-01. Several 8.4
# defaults are CPU-derived (innodb_read_io_threads is logical-processors/2,
# floor 4; purge threads and buffer-pool instances likewise), so measuring on
# a multi-core dev host reports values the droplet will never take.
# `--cpus` alone is NOT enough: it is a CFS quota, so mysqld still sees every
# host processor via sysconf and still derives the multi-core value.
# `--cpuset-cpus` is the usual answer, but it does NOT work here either:
# Docker Desktop runs a Linux VM, and mysqld reads the VM's processor count,
# so `innodb_read_io_threads` still resolves to the multi-core value.
# ⚠ CONSEQUENCE: the CPU-derived defaults below CANNOT be measured for the
# droplet from this machine. Read them ON the box:
#   SELECT @@innodb_read_io_threads, @@innodb_purge_threads,
#          @@innodb_buffer_pool_instances, @@innodb_parallel_read_threads;
#
# Usage:  bash infra/rehearse-84-upgrade.sh /path/to/rendered.cnf
set -uo pipefail

CFG="${1:-}"
VOL=tbd360_rehearsal
PW=rehearsal_only_not_a_secret

cleanup() { docker rm -f r80 r84 >/dev/null 2>&1; }
trap cleanup EXIT

if [[ -z "$CFG" ]]; then
  echo "!! pass the path to a RENDERED my.cnf (jinja expanded), so this tests the real config"
  echo "   e.g. bash $0 /tmp/rendered.cnf"
  exit 2
fi

echo "=== 0. config validates on BOTH engines (it lands on the live 8.0 box first) ==="
for v in 8.0 8.4; do
  printf "    mysql:%-4s " "$v"
  if docker run --rm --entrypoint mysqld -v "$CFG":/etc/mysql/conf.d/pfv.cnf:ro "mysql:$v" \
       --validate-config >/tmp/vc_$v.txt 2>&1; then echo "validate-config exit 0"
  else echo "FAILED"; head -3 /tmp/vc_$v.txt; exit 1; fi
done

docker rm -f r80 r84 >/dev/null 2>&1
docker volume rm -f "$VOL" >/dev/null 2>&1
docker volume create "$VOL" >/dev/null

echo "=== 1. boot 8.0 on a fresh datadir with that config ==="
docker run -d --name r80 --cpus=1 --cpuset-cpus=0 -m 2g -e MYSQL_ROOT_PASSWORD="$PW" -e MYSQL_DATABASE=pfv2 \
  -v "$VOL":/var/lib/mysql -v "$CFG":/etc/mysql/conf.d/pfv.cnf:ro mysql:8.0 >/dev/null
# NB: `mysqladmin ping` answers even on access-denied, so it is not a readiness
# probe. Use a real authenticated query.
for i in $(seq 1 60); do docker exec r80 mysql -uroot -p"$PW" -N -e "SELECT 1" >/dev/null 2>&1 && break; sleep 3; done
docker exec r80 mysql -uroot -p"$PW" -N -e "SELECT CONCAT('    running ', VERSION())" 2>/dev/null

echo "=== 2. load the constructs a DD upgrade actually re-parses ==="
docker exec -i r80 mysql -uroot -p"$PW" pfv2 >/dev/null 2>&1 <<'SQL'
CREATE TABLE organizations (
  id INT PRIMARY KEY AUTO_INCREMENT,
  name VARCHAR(200) NOT NULL,
  settings JSON NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
-- Mirrors alembic 034: STORED generated column, non-default collation, UNIQUE.
ALTER TABLE organizations
  ADD COLUMN name_normalized VARCHAR(200)
  GENERATED ALWAYS AS (LOWER(name)) STORED
  COLLATE utf8mb4_0900_as_cs;
ALTER TABLE organizations ADD CONSTRAINT uq_organizations_name_normalized UNIQUE (name_normalized);
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
-- Mirrors alembic 036: named CHECK carrying an expression.
ALTER TABLE transactions ADD CONSTRAINT ck_transactions_settled_implies_settled_date
  CHECK (status <> 'settled' OR settled_date IS NOT NULL);
INSERT INTO organizations (name, settings) VALUES ('Acme Household', '{"currency":"EUR"}');
INSERT INTO transactions (org_id, amount, status, settled_date, payload)
  VALUES (1, 6500.00, 'settled', '2026-03-25', '{"source":"salary"}'), (1, 42.55, 'pending', NULL, NULL);
CREATE USER 'pfv_app'@'%' IDENTIFIED WITH caching_sha2_password BY 'app-pw';
GRANT ALL ON pfv2.* TO 'pfv_app'@'%';
SQL

echo "=== 3. capture the 8.0 baseline ==="
BASE=$(docker exec r80 mysql -uroot -p"$PW" -N -e "
SELECT CONCAT('tables=',(SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='pfv2'),
' rows=',(SELECT COUNT(*) FROM pfv2.transactions),
' sum=',(SELECT SUM(amount) FROM pfv2.transactions),
' gen=',(SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='pfv2' AND extra LIKE '%GENERATED%'),
' chk=',(SELECT COUNT(*) FROM information_schema.table_constraints WHERE constraint_schema='pfv2' AND constraint_type='CHECK'));" 2>/dev/null)
echo "    8.0: $BASE"
echo "    8.0 dd version: $(docker exec r80 mysql -uroot -p"$PW" -N -e "SELECT properties FROM mysql.dd_properties" 2>/dev/null | head -c 120)"
docker exec r80 mysql -uroot -p"$PW" -N -e "SHOW GLOBAL VARIABLES" 2>/dev/null | sort > /tmp/vars_80.txt

echo "=== 4. slow shutdown (the step the runbook requires) ==="
docker exec r80 mysql -uroot -p"$PW" -e "SET GLOBAL innodb_fast_shutdown = 0;" 2>/dev/null
docker exec r80 mysqladmin -uroot -p"$PW" shutdown 2>/dev/null
sleep 6; docker rm -f r80 >/dev/null 2>&1

echo "=== 5. START 8.4 ON THE SAME DATADIR — the in-place upgrade ==="
docker run -d --name r84 --cpus=1 --cpuset-cpus=0 -m 2g -e MYSQL_ROOT_PASSWORD="$PW" \
  -v "$VOL":/var/lib/mysql -v "$CFG":/etc/mysql/conf.d/pfv.cnf:ro mysql:8.4 >/dev/null
for i in $(seq 1 60); do docker exec r84 mysql -uroot -p"$PW" -N -e "SELECT 1" >/dev/null 2>&1 && break; sleep 3; done
docker logs r84 2>&1 | grep -E "MY-011090|MY-013413|MY-013381" | sed 's/^/    /'

echo "=== 6. did the risky constructs survive? ==="
AFTER=$(docker exec r84 mysql -uroot -p"$PW" -N -e "
SELECT CONCAT('tables=',(SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='pfv2'),
' rows=',(SELECT COUNT(*) FROM pfv2.transactions),
' sum=',(SELECT SUM(amount) FROM pfv2.transactions),
' gen=',(SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='pfv2' AND extra LIKE '%GENERATED%'),
' chk=',(SELECT COUNT(*) FROM information_schema.table_constraints WHERE constraint_schema='pfv2' AND constraint_type='CHECK'));" 2>/dev/null)
echo "    8.4: $AFTER"
if [ -z "$AFTER" ] || [ "$AFTER" != "$BASE" ]; then
  echo "    !! INTEGRITY MISMATCH — 8.0 was: $BASE"
else
  echo "    integrity: identical to the 8.0 baseline"
fi
echo "    generated-column collation still as_cs:"
docker exec r84 mysql -uroot -p"$PW" -N -e "
SELECT CONCAT('      ', column_name,' ',collation_name,' ',generation_expression)
FROM information_schema.columns WHERE table_schema='pfv2' AND extra LIKE '%GENERATED%';" 2>/dev/null
echo "    CHECK constraint still enforced (this INSERT must FAIL):"
if docker exec r84 mysql -uroot -p"$PW" pfv2 -e \
   "INSERT INTO transactions (org_id,amount,status,settled_date) VALUES (1,1.00,'settled',NULL);" >/dev/null 2>&1
then echo "      !! CHECK NOT ENFORCED — the constraint did not survive the upgrade"; else echo "      OK, rejected"; fi
echo "    UNIQUE on the generated column still enforced (this INSERT must FAIL):"
if docker exec r84 mysql -uroot -p"$PW" pfv2 -e \
   "INSERT INTO organizations (name) VALUES ('ACME HOUSEHOLD');" >/dev/null 2>&1
then echo "      !! UNIQUE NOT ENFORCED"; else echo "      OK, rejected"; fi
echo "    JSON readable:"
docker exec r84 mysql -uroot -p"$PW" -N -e \
  "SELECT CONCAT('      ', JSON_EXTRACT(settings,'\$.currency')) FROM pfv2.organizations LIMIT 1;" 2>/dev/null
echo "    non-TLS caching_sha2 login (protocol only; NOT proof of production's keypair):"
docker exec r84 mysql -h127.0.0.1 --ssl-mode=DISABLED --get-server-public-key -upfv_app -papp-pw -N \
  -e "SELECT '      OK';" 2>/dev/null

echo "=== 7. variable delta, 8.0 -> 8.4, WITH this config applied ==="
docker exec r84 mysql -uroot -p"$PW" -N -e "SHOW GLOBAL VARIABLES" 2>/dev/null | sort > /tmp/vars_84.txt
python3 - <<'PY'
def load(f):
    d = {}
    for l in open(f):
        if "\t" in l:
            k, v = l.rstrip("\n").split("\t", 1); d[k] = v
    return d
a, b = load("/tmp/vars_80.txt"), load("/tmp/vars_84.txt")
changed = {k: (a[k], b[k]) for k in a.keys() & b.keys() if a[k] != b[k]}
print(f"    value changes on names present in BOTH: {len(changed)}")
print(f"    present only on 8.0 (REMOVED in 8.4): {sorted(a.keys() - b.keys())}")
print(f"    present only on 8.4 (NEW): {sorted(b.keys() - a.keys())}")
for k in sorted(changed):
    print(f"      {k:<42} {changed[k][0]:>12} -> {changed[k][1]}")
PY
echo
echo "NOTE: this delta is measured WITH the config applied, so every value the"
echo "config pins is masked by construction. If Oracle's packaging does not"
echo "include /etc/mysql/mysql.conf.d, the real delta is LARGER and includes"
echo "innodb_io_capacity 200 -> 10000. That is what runbook section 5 gates on."
