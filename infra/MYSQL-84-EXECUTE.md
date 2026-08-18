# TBD-360 — execution sheet (copy/paste)

**This is the do-it-now sheet.** For *why* any step exists, read
[`MYSQL-84-CUTOVER.md`](MYSQL-84-CUTOVER.md); for the analysis, the spec beside
it. This file assumes you have already decided to go and are sitting at your
laptop with `doctl`, `terraform`, `ansible` and your SSH key.

**Total expected outage: ~15–25 min**, of which the database is ~1 minute
(measured 49s on a real droplet, 2026-08-18). The window is dominated by App
Platform scaling and the cold snapshot.

> **No production identifiers appear in this file.** Every IP, droplet id and
> app id is derived at run time. That keeps it safe in a public repo *and*
> keeps it correct if anything is ever rebuilt.

---

## Conventions

- Every block is copy/paste as-is into **one** terminal, in order.
- `✅ EXPECT:` lines tell you what a good result looks like. **If you do not see
  it, stop.** Do not proceed on "probably fine".
- 🌐 marks a **web-console fallback** for steps that can be done without the CLI.
- 🔙 marks a rollback point.

---

## Phase 0 — Session setup (no outage, do this first)

```bash
cd ~/src/tbd
git checkout main && git pull --ff-only

export TFDIR=infra/terraform
export DROPLET_ID=$(terraform -chdir=$TFDIR output -raw droplet_id)
export DROPLET_IP=$(terraform -chdir=$TFDIR output -raw droplet_public_ipv4)
export APP_ID=$(doctl apps list --format ID,Spec.Name --no-header | awk '$2=="pfv"{print $1}')
export SSH_KEY=~/.ssh/id_rsa.home
export SSHQ="ssh -o BatchMode=yes -i $SSH_KEY root@$DROPLET_IP"

echo "droplet=$DROPLET_ID ip=$DROPLET_IP app=$APP_ID"
```

✅ EXPECT: three non-empty values. An empty `APP_ID` means `doctl` is
unauthenticated — fix that before going further.

### 0.1 Prove every access path works *now*, not mid-window

```bash
$SSHQ 'echo SSH_OK; mysql --no-defaults -N -B -e "SELECT VERSION()"'
terraform -chdir=$TFDIR output -json | python3 -c 'import json,sys; d=json.load(sys.stdin); print("TF secrets present:", all((d.get(k) or {}).get("value") for k in ("mysql_app_password","mysql_backup_password","redis_password")))'
doctl compute droplet backups $DROPLET_ID --format Name,Created --no-header | head -3
```

✅ EXPECT: `SSH_OK`, `8.0.46-...`, `TF secrets present: True`, and **at least one
backup row**.

⚠ If there is no backup row, **stop**. Enabling backups creates nothing; the
policy is `daily, hour 0`, so you may be waiting until the next 00:00 UTC.

### 0.2 Record your rollback coordinates

```bash
doctl compute droplet backups $DROPLET_ID --format ID,Name,Created --no-header | head -1
```

📋 **Write the backup ID down.** A *backup* restores with
`doctl compute droplet-action restore --image <id>`; a *snapshot* (Phase 2) uses
`rebuild`. Both preserve the droplet id and both IPv4s. Reaching for the wrong
verb mid-outage is a hard stop.

### 0.3 Dry run — changes nothing

```bash
./infra/ansible/bin/run-playbook.sh --production --check --diff
```

✅ EXPECT: `failed=0`. Some `changed` is normal — the three user tasks rewrite
every run by design (`plugin_auth_string` is cleartext and cannot be compared).

---

## Phase 1 — Credentials + config  ⚠ SHORT OUTAGE

This converts the three accounts to `caching_sha2_password`, removes the
`default-authentication-plugin` line 8.4 refuses to boot with, and **rotates the
passwords to the Terraform-generated values**. The app keeps using the old
password until 1.2 — expect a gap.

```bash
./infra/ansible/bin/run-playbook.sh --production
```

✅ EXPECT: `failed=0`, and the play's own final task
`Assert the RUNNING server has the intended configuration` passing. That task
reads the live server back — if it passes, the config is genuinely in effect,
not merely on disk.

### 1.1 Verify by failure mode, not by allowlist

```bash
$SSHQ 'mysql --no-defaults -t -e "SELECT user, host, plugin, LENGTH(authentication_string) hash_len FROM mysql.user WHERE plugin <> \"caching_sha2_password\""'
$SSHQ 'mysql --no-defaults -t -e "SELECT user, host, LENGTH(authentication_string) hash_len FROM mysql.user WHERE user LIKE \"pfv%\""'
$SSHQ 'grep -rn "default.authentication.plugin" /etc/mysql/ || echo "  ABSENT (good)"'
```

✅ EXPECT: first query returns **only `root@localhost / auth_socket`**. Second
shows `hash_len = 70` for all three `pfv_*` rows — a `0` means the passwordless
form got in. Third prints `ABSENT` (comments do not count; look for a real
setting line).

⚠ Anything else in the first query — a monitoring user, a hand-made login — is
an account that **stops authenticating the moment 8.4 starts**, mid-window,
after the point of no return. Convert it now or delete it.

### 1.2 Re-encrypt BOTH `DATABASE_URL` bindings

⚠⚠ **There are two, and they are separate encrypted values**: the `backend`
service and the `migrate` PRE_DEPLOY job. Missing the job's copy does not fail
now — it fails at the **next deploy**. Same for `REDIS_URL`.

```bash
terraform -chdir=$TFDIR output -raw mysql_app_password   # copy, do not echo into notes
terraform -chdir=$TFDIR output -raw redis_password
terraform -chdir=$TFDIR output -raw droplet_private_ipv4
```

🌐 **Console (recommended here — fewer moving parts):**
Apps → `pfv` → Settings → `backend` → Environment Variables → edit
`DATABASE_URL` and `REDIS_URL` → **repeat for the `migrate` job** → Save. Saving
triggers a deploy.

**CLI alternative:** put the plaintext values into `.do/app.yaml`, then
`doctl apps update $APP_ID --spec .do/app.yaml`. DigitalOcean re-encrypts them
and returns `EV[...]`; fetch the spec back and commit those blobs
(`doctl apps spec get $APP_ID > .do/app.yaml`). **Never commit plaintext.**

```bash
curl -s https://$(doctl apps get $APP_ID --format DefaultIngress --no-header | sed 's|https://||')/ready
```

✅ EXPECT: `database: connected`.

🔙 **ROLLBACK (still cheap here):** on 8.0 you can put the accounts back with
`ALTER USER 'pfv_app'@'%' IDENTIFIED WITH mysql_native_password BY '<old>';` and
restore the previous secrets. Nothing irreversible has happened yet.

---

## Phase 2 — Quiesce and snapshot  ⚠ OUTAGE STARTS

### 2.1 Scale the backend to zero

🌐 **Console:** Apps → `pfv` → `backend` → Resize → instance count **0**.

⚠ Do not skip this. Any write taken after the snapshot is **lost** on rollback,
and a live backend would serve writes straight through the package swap.

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://$(doctl apps get $APP_ID --format DefaultIngress --no-header | sed 's|https://||')/health
```

✅ EXPECT: a failure (`000`, `502`, `503`). A `200` means it is still serving —
do not continue.

### 2.2 Cold snapshot, powered off

```bash
doctl compute droplet-action power-off $DROPLET_ID --wait
doctl compute droplet-action snapshot $DROPLET_ID --snapshot-name "tbd360-pre-84-$(date +%Y%m%d-%H%M)" --wait
doctl compute droplet-action power-on $DROPLET_ID --wait
doctl compute snapshot list --resource droplet --format ID,Name,Created --no-header | head -2
```

📋 **Write the snapshot ID down.** This is your primary undo from here on.
Expect roughly 5–10 minutes on a 25 GB disk.

🔙 **ROLLBACK from here on:**
`doctl compute droplet-action rebuild $DROPLET_ID --image <snapshot-id> --wait`
(**`rebuild`** for a snapshot; `restore` is for a backup). Preserves the droplet
id and both IPv4s, so the private address App Platform is pinned to survives.

---

## Phase 3 — The cutover  ⚠⚠ ONE-WAY DOOR

**In-place downgrade from 8.4 to 8.0 is not supported.** Past this point the
only way back is the snapshot.

### 3.1 Slow shutdown

⚠⚠ **`--no-defaults` is mandatory.** `/root/.my.cnf` forces `user = pfv_backup`,
so a bare `mysql` as root authenticates as a low-privilege account and this
step fails with `ERROR 1227` — then dpkg stops mysqld with the *default fast*
shutdown and the DD upgrade starts from a non-clean state. It looks survivable
and is exactly the failure this step exists to prevent.

```bash
$SSHQ 'mysql --no-defaults -N -B -e "SELECT CURRENT_USER()"'
$SSHQ 'mysql --no-defaults -e "SET GLOBAL innodb_fast_shutdown = 0;" && echo SET_OK'
$SSHQ 'mysqladmin --no-defaults shutdown; sleep 5; systemctl is-active mysql || echo "STOPPED (good)"'
```

✅ EXPECT: `root@localhost`, then `SET_OK`, then `STOPPED (good)`.
⚠ If the first line prints `pfv_backup@localhost`, your `--no-defaults` did not
take — stop and fix it before shutting anything down.

### 3.2 Package swap to Oracle 8.4

⚠ Use `mysql-apt-config`. Do **not** hand-add the repo key: the standalone
`RPM-GPG-KEY-mysql-2023` expired 2025-10-22 and apt then rejects the repo with
`EXPKEYSIG`, which surfaces as the very misleading
`E: Unable to locate package mysql-community-server`.

```bash
$SSHQ 'export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq lsb-release wget gnupg debconf debconf-utils >/dev/null
curl -fsSL -o /tmp/mac.deb http://repo.mysql.com/apt/ubuntu/pool/mysql-apt-config/m/mysql-apt-config/mysql-apt-config_0.8.39-1_all.deb
cat <<SEL | debconf-set-selections
mysql-apt-config mysql-apt-config/repo-distro select ubuntu
mysql-apt-config mysql-apt-config/repo-codename select noble
mysql-apt-config mysql-apt-config/repo-url string http://repo.mysql.com/apt
mysql-apt-config mysql-apt-config/select-server select mysql-8.4-lts
mysql-apt-config mysql-apt-config/select-connectors select Disabled
mysql-apt-config mysql-apt-config/select-product select Ok
SEL
dpkg -i /tmp/mac.deb && apt-get update -qq
apt-cache policy mysql-community-server | head -3'
```

✅ EXPECT: a real `Candidate:` such as `8.4.11-1ubuntu24.04`, **not `(none)`**.
⚠ `apt-get update` exits **0** against a repo whose signature failed, so its
exit status proves nothing. Gate on the candidate.

```bash
$SSHQ 'export DEBIAN_FRONTEND=noninteractive; apt-get install -y -o Dpkg::Options::=--force-confold mysql-community-server 2>&1 | tail -15'
```

⚠ **Do not run `mysql_upgrade`** — removed in 8.4. The server performs the
data-dictionary upgrade itself at startup.

```bash
$SSHQ 'systemctl start mysql 2>/dev/null || systemctl start mysqld; sleep 5; mysql --no-defaults -N -B -e "SELECT VERSION()"'
```

✅ EXPECT: `8.4.x`. Measured elapsed for 3.1→here on a rehearsal droplet: **49
seconds**.

---

## Phase 4 — Verify before letting traffic back

### 4.1 Config include path — assert BY NAME

⚠ `mysqld --validate-config` **cannot** detect a dropped include: a file that is
never read still validates. Only the live values prove it.

```bash
$SSHQ 'mysql --no-defaults -N -B -e "SELECT CONCAT(@@bind_address,\" | \",@@collation_server,\" | \",@@innodb_buffer_pool_size,\" | \",@@innodb_io_capacity,\"/\",@@innodb_io_capacity_max,\" | redo=\",@@innodb_redo_log_capacity)"'
```

✅ EXPECT exactly:
`0.0.0.0 | utf8mb4_0900_ai_ci | 805306368 | 1000/2000 | redo=268435456`

⚠ `redo=104857600` (100M) means Oracle's packaging dropped
`/etc/mysql/mysql.conf.d` — the config is not applied. `bind_address` of
`127.0.0.1` is the same failure and is the one that breaks App Platform while
your local client keeps working.
⚠ `innodb_doublewrite_pages` will read **128**, not 4. That is correct and
deliberate — it is not pinned.

### 4.2 Auth over the transport the app actually uses

```bash
$SSHQ 'mysql --no-defaults -N -B -e "SELECT user,host,plugin FROM mysql.user WHERE plugin<>\"caching_sha2_password\""'
$SSHQ 'test -f /etc/mysql/debian.cnf && echo "debian.cnf present" || echo "debian.cnf GONE - logrotate will fail silently, file a ticket"'
```

✅ EXPECT: only `root@localhost auth_socket`.

⚠ A plain `mysql -h 127.0.0.1` negotiates TLS and never exercises the RSA
public-key path `caching_sha2_password` needs over the no-TLS VPC link. It
passes while production still cannot connect. The real proof is `/ready` below.

### 4.3 Bring traffic back

🌐 **Console:** Apps → `pfv` → `backend` → Resize → instance count **1**.

```bash
curl -s https://$(doctl apps get $APP_ID --format DefaultIngress --no-header | sed 's|https://||')/ready
```

✅ EXPECT: `database: connected`. Then log in through the UI and load one real
page — `/ready` proves connectivity, not that the app works.

---

## Phase 5 — Rename `pfv2` → `tbd`  (separate phase, never interleaved)

Run this **only after Phase 4 is green**. Two reasons: the upgrade is
effectively irreversible while the rename is trivially reversible (rename back),
and keeping them sequential means a failure is immediately attributable to one
change.

### 5.1 Re-verify the assumption that makes it cheap

```bash
$SSHQ 'mysql --no-defaults -t -e "SELECT (SELECT COUNT(*) FROM information_schema.views WHERE table_schema=\"pfv2\") views, (SELECT COUNT(*) FROM information_schema.triggers WHERE trigger_schema=\"pfv2\") trgs, (SELECT COUNT(*) FROM information_schema.routines WHERE routine_schema=\"pfv2\") routs, (SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=\"pfv2\" AND table_type=\"BASE TABLE\") tables"'
```

✅ EXPECT: `0 | 0 | 0 | 50`. Views, triggers and routines do **not** move with
`RENAME TABLE`; their absence is what makes this a complete operation rather
than a partial one. A non-zero count means stop and re-plan.

### 5.2 Generate and run the rename — one atomic statement

⚠ There are **83 FK declarations**. Renaming table-by-table would leave foreign
keys pointing at tables not yet moved and fail partway with the schema
half-renamed. MySQL renames all pairs atomically in a single statement.

```bash
./infra/ansible/bin/gen-rename-sql.sh --host $DROPLET_IP --from pfv2 --to tbd > /tmp/rename.sql
head -3 /tmp/rename.sql; echo '...'; grep -c '^  pfv2\.' /tmp/rename.sql
```

✅ EXPECT the table count to equal **50**. The generator asserts this itself and
refuses to emit a truncated list.

```bash
$SSHQ 'mysql --no-defaults' < /tmp/rename.sql
$SSHQ 'mysql --no-defaults -N -B -e "SELECT CONCAT(schema_name) FROM information_schema.schemata WHERE schema_name IN (\"pfv2\",\"tbd\")"'
$SSHQ 'mysql --no-defaults -N -B -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=\"tbd\""'
```

✅ EXPECT: `tbd` present, 50 tables. `alembic_version` moves with the rest, so
alembic continues from the same head with no stamping.

### 5.3 Grants and secrets follow the rename

```bash
$SSHQ 'mysql --no-defaults -e "GRANT ALL PRIVILEGES ON tbd.* TO \"pfv_app\"@\"%\"; GRANT ALL PRIVILEGES ON tbd.* TO \"pfv_app\"@\"localhost\"; GRANT SELECT, LOCK TABLES, SHOW VIEW, EVENT, TRIGGER, RELOAD, REPLICATION CLIENT ON *.* TO \"pfv_backup\"@\"localhost\"; FLUSH PRIVILEGES;"'
```

⚠⚠ Then update **both** `DATABASE_URL` bindings again — the database name is in
the URL. Service **and** migrate job. Missing the job's copy fails at the next
deploy, not now.

```bash
$SSHQ 'grep -n "pfv2" /usr/local/bin/mysql-backup.sh 2>/dev/null || echo "  backup script: no pfv2 reference"'
```

⚠ If the nightly backup script names `pfv2`, it silently starts dumping nothing.
Update it, or re-run the play once `mysql_app_db` is changed to `tbd`.

🔙 **ROLLBACK:** rename back. `RENAME TABLE tbd.x TO pfv2.x, ...` and restore
the previous `DATABASE_URL`s.

---

## Phase 6 — Close out

```bash
# 1. Prove the nightly backup still works, by hand
$SSHQ '/usr/local/bin/mysql-backup.sh && ls -lh /var/backups/mysql/ | tail -3'

# 2. Confirm the migrate job can still reach the DB (it has its OWN DATABASE_URL)
doctl apps create-deployment $APP_ID --wait
doctl apps logs $APP_ID migrate --type run | tail -5
```

✅ EXPECT a non-empty dump, and `migrate.no_op` (or `migrate.complete`) naming
the **new** database.

⚠ The nightly backup has four known holes — on-disk only, one database only so
**grants are not backed up**, `gzip >` creates the file before `mysqldump` can
fail so existence ≠ success, and no alerting. Tracked as **TBD-400**.

### Then, as separate PRs

| Change | Where |
|---|---|
| `enable_backups = true` → `false` | `infra/terraform/main.tf` (TBD-399's stated revert) — **already prepared, see the open PR** |
| "production still on 8.0 pending the TBD-360 cutover" | `README.md`, `CLAUDE.md` |
| Add production as the final evidence row | `MYSQL-84-CUTOVER.md` |
| Decide whether CI keeps the `mysql: ["8.0","8.4"]` matrix | `.github/workflows/test.yml` |
| Fill in the outcome record | `specs/2026-08-18-mysql-84-cutover-record.md` |

```bash
# Destroy the pre-cutover snapshot once you are confident (it holds a full copy
# of production). Keep it at least a few days.
doctl compute snapshot delete <snapshot-id> --force
```

---

## If it goes wrong

| Symptom | Meaning | Action |
|---|---|---|
| App cannot authenticate after Phase 1 | `DATABASE_URL` not updated, or updated on only one of the two bindings | Fix the secret; no rollback needed |
| `E: Unable to locate package mysql-community-server` | Expired signing key, **not** a wrong component name | Use `mysql-apt-config`, never the standalone `-2023` key |
| `ERROR 1227 ... SUPER` | You dropped `--no-defaults`; you are `pfv_backup` | Re-run with `--no-defaults` |
| 8.4 will not start | `default-authentication-plugin` still in a config file | `grep -rn` it out, restart |
| `redo=104857600` / `bind_address=127.0.0.1` after cutover | `/etc/mysql/mysql.conf.d` no longer included | Re-add `!includedir` to `/etc/mysql/my.cnf`, restart |
| Anything worse | — | `doctl compute droplet-action rebuild $DROPLET_ID --image <snapshot-id> --wait` |

⚠ After **any** rebuild/restore, the accounts are back on
`mysql_native_password` and the secrets are back to the old values. Re-run
Phase 1 before trying again.

**In-8.4 escape hatch:** if an unconverted account is discovered mid-window,
`mysql_native_password` can be re-enabled on 8.4 with
`--mysql-native-password=ON`. It is removed entirely in 9.0, so it buys time,
not a resting place.
