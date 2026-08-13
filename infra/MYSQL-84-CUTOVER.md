# MySQL 8.0 → 8.4 cutover runbook (TBD-360)

The repo-side pre-flight is done and verified. What remains touches the live
droplet, and every step below is an **operator** action: the agent loop does not
write to the production database and does not run the cutover.

Full detail: `specs/2026-08-09-mysql-84-lts-upgrade.md`. **This file does not
replace the spec** — it is the ordered checklist, and it names the spec where a
step has detail worth reading in full before the window.

---

## What is already proven, and how

| Claim | How it was verified |
|---|---|
| 8.4 refuses to start on the current config | `mysqld --validate-config` on real `mysql:8.4`: exit 1, `unknown variable 'default-authentication-plugin=mysql_native_password'` |
| Removing that line is rollback-safe | Same check on `mysql:8.0` with the fixed config: exit 0. The fix lands on the 8.0 box, which must still start |
| `mysql_native_password` is unusable on 8.4 | `PLUGIN_STATUS` = `DISABLED`; `CREATE USER ... IDENTIFIED WITH mysql_native_password` → `ERROR 1524 Plugin ... is not loaded` |
| Accounts convert in place on 8.0 and still authenticate | `ALTER USER ... IDENTIFIED WITH caching_sha2_password BY '<pw>'` on real 8.0, then login OK |
| Ansible converts an EXISTING user correctly, with `plugin_auth_string` | Real `community.mysql` against real 8.0: `plugin_auth_string` → `hash_len=70`, login OK. `password:` → `hash_len=0`, `ERROR 1045`. See the warning below |
| Schema applies on 8.4 | Real 8.4.11: all **80** alembic revisions to head, idempotent re-run, `utf8mb4_0900_ai_ci` preserved, `/ready` → 200 `database: connected` |
| The driver stack works on 8.4 | `cryptography 44.0.3` present **in the image**, `aiomysql 0.2.0`, `PyMySQL 1.1.3`; app authenticates with `caching_sha2_password` |
| CI executes migrations against 8.4 on a real runner | `Migration Checks` job, now matrixed over 8.0 **and** 8.4 |
| **The in-place upgrade itself works** | Rehearsed: an 8.0.46 datadir, slow-shutdown, then started under 8.4.11 with the final config. `Data dictionary upgrading from '80023' to '80300' ... completed`, `Server upgrade from '80046' to '80411' completed`, row counts and a DECIMAL sum identical, accounts still on `caching_sha2_password` with 70-byte hashes, **zero** deprecation warnings, and a non-TLS `--get-server-public-key` login OK |
| 8.4's re-defaults are known, not guessed | Same datadir booted under both, `SHOW GLOBAL VARIABLES` diffed: exactly **10** InnoDB/memory/thread knobs move, and the io_capacity pins hold at 1000/2000 |

⚠ **What is NOT evidence, so nobody re-derives false confidence from it.** The
backend test suite ran green against an 8.4 stack (4106 passed), and that proves
nothing about 8.4: the suite is in-process aiosqlite throughout, so it would
produce exactly that result with no MySQL container running at all. The real
8.4 evidence is the `Migration Checks` job, the alembic run, and `/ready`.

⚠ **The thing that would have taken production down.** Flipping
`plugin: mysql_native_password` → `caching_sha2_password` in the Ansible role is
NOT sufficient and fails silently. `community.mysql.mysql_user` **ignores
`password:` when `plugin:` is set** — it writes the plugin and leaves
`authentication_string` EMPTY, reporting `changed: true` and succeeding either
way:

```
plugin + password:            plugin=caching_sha2_password, hash_len=0   -> ERROR 1045 Access denied
plugin + plugin_auth_string:  plugin=caching_sha2_password, hash_len=70  -> login OK
```

The role uses `plugin_auth_string`. Do not "simplify" it back. The collection is
capped below 4.0.0 in `requirements.yml` for the same reason.

---

## Order of operations

### 0. Gate: a restorable backup must EXIST

`enable_backups = false` in `infra/terraform/main.tf` overrides the module's own
`default = true`. **TBD-399** flips it.

⚠ **"TBD-399 applied" is not the gate. "A backup exists" is.** Enabling backups
creates nothing; DO takes them on its own schedule, so the first restorable
image may be days after the apply. Gate on output, not on the apply:

```bash
doctl compute droplet backups <droplet-id>     # must return at least one row
```

⚠ **Backups and snapshots restore with different verbs.** A *backup* is applied
with `doctl compute droplet-action restore --image <backup-id>`; a *snapshot*
(step 3) is applied with `doctl compute droplet-action rebuild --image
<snapshot-id>`. Both preserve the droplet id and both IPv4s, so the private IP
App Platform is pinned to survives either way. Record the exact id and the exact
verb before the window; reaching for the wrong one mid-outage is a hard stop.

### 1. Apply the repo changes to the 8.0 box — ⚠ SCHEDULE A SHORT WINDOW

This is **not** non-destructive. Running the play on the current 8.0 server:

- rewrites three live credentials, and
- changes `my.cnf.j2`, which fires `notify: Restart mysql` — so it **restarts
  production MySQL** at the end of the play. On a single-node data plane with no
  replica that is a real outage window, plus a full `caching_sha2` fast-auth
  cache flush that pushes every reconnect through RSA key retrieval at once.

**Precondition, before running anything.** The play now rewrites
`mysql_app_password` unconditionally (see the idempotency note in the role). If
the vaulted inventory password has ever drifted from the App Platform
`DATABASE_URL` secret, the play silently replaces the working credential and
every App Platform connection dies. Confirm they match **first**:

```bash
# compare the inventory value against the live DATABASE_URL secret before the play
```

Then run the play, and verify with the **failure-mode** query — not an
allowlist of the accounts Ansible just fixed:

```sql
SELECT user, host, plugin, LENGTH(authentication_string) AS hash_len
FROM mysql.user
WHERE plugin <> 'caching_sha2_password';
```

⚠ Expected: **only** `root@localhost` on `auth_socket`, and nothing else. Any
other row — a monitoring user, a hand-made operator login, `debian-sys-maint` —
is an account that stops authenticating the moment 8.4 starts, mid-window,
after the point of no return. A query scoped to `pfv_app`/`pfv_backup` returns
three green rows and certifies the gap; that is why it is not used here.

Then confirm `hash_len` is **70** for the three app accounts. A `0` means the
passwordless form got in: stop, the app is already down.

**Verify over the transport the app actually uses.** The app connects over the
VPC with **no TLS**, so `caching_sha2_password` needs RSA public-key retrieval.
A plain `mysql -h 127.0.0.1` defaults to `--ssl-mode=PREFERRED`, negotiates TLS,
and never touches that path — so it passes even when production cannot connect:

```bash
mysql -h 127.0.0.1 --ssl-mode=DISABLED --get-server-public-key -u pfv_app -p -e "SELECT 1;"
mysql -u pfv_app -p -e "SELECT 1;"                  # socket, exercises the @localhost row
mysqldump -u pfv_backup -p --single-transaction --databases pfv2 > /tmp/verify.sql   # socket; no RSA needed
```

Finally hit `/ready` and read the backend logs. `hash_len = 70` proves a hash
exists, not that it is the password the app holds.

⚠ The play is **no longer idempotent** on those three tasks
(`plugin_auth_string` is cleartext, so it rewrites every run and always reports
`changed`). Expected — and it means `changed: false` is no longer available as
evidence that inventory and server agree, which is why the precondition above
exists.

### 2. Test-restore a real dump into a throwaway 8.4 instance

An untested restore is not a rollback plan.

⚠ The nightly artifact is `pfv2_<date>.sql.gz`, gzipped, and dumped **without**
`--databases` — so it contains no `CREATE DATABASE` and no `USE`. It must be
restored into a named database or it dies with `ERROR 1046 No database selected`:

```bash
docker run -d --name restore-probe -e MYSQL_ROOT_PASSWORD=... -e MYSQL_DATABASE=pfv2 mysql:8.4
# wait for readiness - first boot init takes tens of seconds
until docker exec restore-probe mysqladmin ping -uroot -p... --silent; do sleep 3; done
zcat pfv2_<date>.sql.gz | docker exec -i restore-probe mysql -uroot -p... pfv2
docker exec restore-probe mysql -uroot -p... -e "SELECT COUNT(*) FROM pfv2.transactions;"
```

Compare row counts against production before trusting it.

⚠ **This is a logical restore into a fresh 8.4, which is NOT the operation the
cutover performs.** The cutover is an in-place data-dictionary upgrade of the
existing datadir. That operation **has now been rehearsed** (see the evidence
table): an 8.0.46 datadir, slow-shutdown, started under 8.4.11 with the final
config — DD upgraded `80023 → 80300`, server `80046 → 80411`, data identical,
accounts and auth intact.

⚠ **What the rehearsal did NOT cover**, and is still worth doing on a scratch
droplet before the window: a **representative synthetic schema** was used, not
a restore of the production dataset, and the rehearsal swapped the *binary*
(container image) rather than performing the **Ubuntu → Oracle package swap**,
which is where `debian-sys-maint`, AppArmor, the systemd unit and the config
include path actually change. The engine-level upgrade path is proven; the
packaging path is not.

### 3. Quiesce the app, then snapshot

1. **Scale the App Platform `backend` to 0** (spec step 8; `infra/MIGRATION.md`
   has the tested procedure: console → `backend` → Resize → instance count 0).
   Confirm `/health` is unavailable before proceeding.
   ⚠ Skipping this means the app serves **writes** through the package swap,
   and any write taken after the snapshot is **lost** on rollback.
2. Cold snapshot with the droplet powered off.

### 4. Cutover

1. `SET GLOBAL innodb_fast_shutdown = 0;` then a clean `mysqladmin shutdown`.
   ⚠ **Required** for an in-place upgrade (spec step 10). Letting dpkg stop
   mysqld with the default fast shutdown starts the DD upgrade from a non-clean
   state.
2. Replace Ubuntu's `mysql-server-8.0` with Oracle's `mysql-community-server`
   via `mysql-apt-config`.
3. ⚠ **Do not run `mysql_upgrade`** — removed in 8.4.

Consequences the spec enumerates and that need handling, each with its own
check: no `debian-sys-maint` / `/etc/mysql/debian.cnf` (Ubuntu's logrotate
authenticates with it, so the slow query log silently stops rotating and fills
the disk days later), different config layout, different AppArmor profile,
different systemd unit, and unattended-upgrades needs a hold so it cannot
reinstall 8.0 over the top.

**Validate the config before the window, not during it.** `mysqld
--validate-config` with the 8.4 binary cannot be run while 8.0 is still
installed — the package swap replaces the server in one dpkg transaction. Do it
the way the pre-flight did: mount the rendered config into a `mysql:8.4`
container and run `--validate-config` there.

### 5. Assert the config actually applied — this is a gate, not a nicety

Everything in `my.cnf.j2` lands in `/etc/mysql/mysql.conf.d/`. If Oracle's
packaging does not include that directory, **the whole file silently stops
applying**: `bind-address` reverts to loopback (App Platform cannot connect
while the operator's local client still works — a slow, ugly diagnosis),
`innodb_buffer_pool_size` drops to 128M, the collation reverts, and the
`innodb_io_capacity` pins that the resource argument depends on disappear.

⚠ `mysqld --validate-config` **cannot catch this**: if the file is not included
it is not read, and validation still exits 0. Diff `SHOW GLOBAL VARIABLES`
before and after and assert by name: `bind_address`, `collation_server`,
`innodb_buffer_pool_size`, `innodb_io_capacity`, `innodb_io_capacity_max`.

**Resolved by measurement, not by blanket pinning.** The spec asked to pin
"~18 InnoDB values 8.4 re-defaults". Booting the same datadir under both
versions and diffing `SHOW GLOBAL VARIABLES` shows **exactly 10** change, and
most of them *reduce* memory on this box — `innodb_adaptive_hash_index` ON→OFF
frees buffer-pool memory, `innodb_change_buffering` all→none frees more,
`innodb_purge_threads` 4→1 is fewer threads on a 1-vCPU box. Pinning those back
would make the upgrade *worse*.

Two changes were acted on, both in `my.cnf.j2` with the measurement inline:

* **`innodb_doublewrite_pages` 4 → 128** is the only knob that increases
  resource use (32x the doublewrite buffer). **Pinned to 4**, so the upgrade
  changes one thing at a time.
* **`innodb_log_file_size` is deprecated on 8.4** and warns on every boot.
  Replaced with `innodb_redo_log_capacity = 256M` — the exact capacity 8.4 was
  computing from the old pair, valid on **both** 8.0 and 8.4, and it removes
  the warning entirely.

⚠ The spec's headline OOM concern — `innodb_io_capacity` re-defaulting 200 →
10000 on a box co-hosting Redis — **is already neutralised** by the explicit
pins that predate this ticket. Measured under 8.4 with this config: still
1000/2000. `temptable_use_mmap` is settable and rollback-safe but is deprecated
in 8.4; pinning it would re-enable behaviour 8.4 deliberately disabled, so it is
left alone.

### 6. After

- Scale `backend` back up (spec step 14)
- `/ready` green, one real authenticated request served
- Run the backup script by hand; confirm a non-empty dump
- ⚠ The nightly backup has four known holes — on-disk only, `pfv2` only so
  **grants are not backed up**, `gzip >` creates the file before `mysqldump`
  fails so existence ≠ success, and no alerting → **TBD-400**

---

## Rollback

In-place downgrade from 8.4 to 8.0 is **not supported**. Do not attempt it.

1. **Snapshot rebuild** (step 3 image) — `doctl compute droplet-action rebuild`.
   Preserves droplet id and both IPv4s. A snapshot taken after step 1 restores
   accounts already on `caching_sha2_password`, which is fine: 8.0 authenticates
   that plugin and the fixed `my.cnf` is 8.0-valid, both measured.
2. **Backup restore** — `doctl compute droplet-action restore`, same
   preservation.
3. **Rebuild from dump onto a NEW droplet** — last resort, and it is not just a
   restore:
   - the private IPv4 changes, so `DATABASE_URL` / `REDIS_URL` (encrypted `EV[]`
     secrets in `.do/app.yaml`) must be re-encrypted, committed and deployed;
   - the DO cloud firewall is Terraform-managed against the droplet id, so a
     hand-built droplet is outside it and 3306 stays blocked until a TFC run
     with manual Confirm & Apply;
   - the nightly dump is `pfv2` only — **users and grants are not in it** — so
     the play must be re-run to recreate `pfv_app`, which re-enters the
     credential-drift precondition in step 1;
   - Valkey shares the droplet, so a full restore rolls back the session and
     rate-limit store too.

**In-8.4 escape hatch:** `mysql_native_password` can be re-enabled on 8.4
(`--mysql-native-password=ON`) if an unconverted account is discovered
mid-window. Removed entirely in 9.0, so it buys time, not a resting place.
