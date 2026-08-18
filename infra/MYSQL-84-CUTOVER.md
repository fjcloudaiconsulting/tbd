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
| The in-place upgrade runs, **on a synthetic schema, via a container-image swap — NOT the Ubuntu→Oracle package swap and NOT a production restore** | `infra/rehearse-84-upgrade.sh` (reproducible). DD `80023 → 80300` and server `80046 → 80411` completed; a STORED generated column keeps `utf8mb4_0900_as_cs` and its expression; a named CHECK and a UNIQUE on the generated column are both still **enforced** (deliberate bad INSERTs rejected); JSON readable. ⚠ Row/sum equality is near-tautological — a DD upgrade does not rewrite tablespaces. ⚠ **Upgrade DURATION on a production-sized datadir was not measured**, so the window is unsized |
| The **Ubuntu → Oracle package swap** completes, and `/etc/mysql/mysql.conf.d` is **still included** by Oracle's packaging | `infra/rehearse-84-scratch-droplet.sh --target local` (reproducible). Real `apt` install of Ubuntu `mysql-server-8.0` **8.0.46** — production's exact version — then the real dpkg transaction to Oracle `mysql-community-server` **8.4.11** on amd64, via the recommended `mysql-apt-config` release package (preseeded through debconf) rather than a hand-written sources list. After the swap all six section-5 variables still hold their configured values (`bind_address 0.0.0.0`, `collation_server utf8mb4_0900_ai_ci`, buffer pool 768M, io_capacity 1000/2000, `innodb_redo_log_capacity 268435456`), the CHECK and the UNIQUE-on-generated-column are still **enforced**, and `debian.cnf` plus the `debian-sys-maint` account both survived. ⚠ A **container**, so systemd and AppArmor are still unrehearsed, and the duration is meaningless (synthetic data). The scratch-droplet run remains the only source of the window size |
| 8.4's re-defaults are enumerated **for a dev host, not for the droplet** | Same datadir under both, `SHOW GLOBAL VARIABLES` diffed: **26** value changes, **15 variables REMOVED** (including `default_authentication_plugin` — so a value-diff alone would have missed this ticket's own root cause), 7 new. io_capacity pins hold at 1000/2000. ⚠ Several 8.4 defaults are CPU-derived and **could not be measured for 1 vCPU** from this machine; read them on the box |

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

`enable_backups` is **now `true`** in `infra/terraform/main.tf` with
`backup_policy { plan = "daily", hour = 0 }` (TBD-399), deliberately temporary
for this migration and reverted to `false` afterwards.

⚠ **"TBD-399 applied" is not the gate. "A backup exists" is.** Enabling backups
creates nothing by itself. TBD-399 sets `plan = "daily", hour = 0`, so the wait
is bounded to **the next 00:00 UTC** rather than DO's default weekly schedule.
That still means applying at 01:00 UTC waits ~23 hours, so time the apply
against the window. Gate on output, not on the apply:

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

**Precondition, before running anything.** The play rewrites
`mysql_app_password` unconditionally (see the idempotency note in the role). If
the vaulted inventory password has ever drifted from the App Platform
`DATABASE_URL` secret, the play silently replaces the working credential and
every App Platform connection dies.

⚠ **You cannot compare them directly.** `DATABASE_URL` is `type: SECRET` in
`.do/app.yaml`; App Platform stores it as `EV[1:...]` and secrets are
**write-only** — the plaintext cannot be read back. An earlier revision of this
runbook said "compare the inventory value against the live secret", which is not
an operation that exists. ⚠ Nor can you compare the two `EV[]` blobs to each
other: they use a per-encryption nonce, so identical plaintext always encrypts
to different ciphertext. Differing blobs mean nothing.

Verify **transitively** instead — the live app proves one side, the inventory
proves the other:

```bash
# 1. The running app proves App Platform's secret matches the CURRENT server password
curl -s https://<app-host>/ready          # expect database: connected

# 2. The inventory holds a real value (prints no secret)
cd infra/ansible
ansible all -m debug -a 'msg={{ "ABORT - default!" if mysql_app_password == "CHANGE_ME" else "set from inventory" }}'

# 3. That value actually authenticates, over the transport the app really uses
ansible all --become -o -m shell -a \
  'MYSQL_PWD="{{ mysql_app_password }}" mysql -h 127.0.0.1 --ssl-mode=DISABLED \
   --get-server-public-key -u pfv_app -e "SELECT 1"'
```

If 1 and 3 both pass, inventory == server == App Platform. `MYSQL_PWD` keeps the
password out of the server's process table; `-p` would put it in `argv`.

⚠ Step 3's flags are not optional. A plain `mysql -h 127.0.0.1` defaults to
`--ssl-mode=PREFERRED`, negotiates TLS, and never exercises the RSA public-key
retrieval that `caching_sha2_password` needs over the no-TLS VPC link — so it
passes while production still cannot connect.

**The roles now fail closed on this.** `roles/mysql` and `roles/redis` assert as
their FIRST task that their secrets are defined, non-empty, and not the
`CHANGE_ME` default. `inventory.yml` is gitignored, so a missing or mis-pointed
`-i` would otherwise let Ansible fall back to the role defaults and set the
production credential to the literal string `CHANGE_ME`, reporting success.

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

⚠⚠ **The scratch-droplet rehearsal is STILL OUTSTANDING and is still the
highest-value remaining pre-flight.** What the container rehearsal did not
cover, and cannot: a **representative synthetic schema** was used, not
a restore of the production dataset, and the rehearsal swapped the *binary*
(container image) rather than performing the **Ubuntu → Oracle package swap**,
which is where `debian-sys-maint`, AppArmor, the systemd unit and the config
include path actually change. The engine-level mechanism is exercised — but that is the half Oracle already
regression-tests. The uncovered half is where every box-specific failure this
runbook enumerates actually lives: `debian-sys-maint`, AppArmor, the systemd
unit, and the config include path in section 5.

### 2a. The scratch-droplet rehearsal, step by step

`infra/rehearse-84-scratch-droplet.sh` performs this. It is one script with two
targets and **one code path** — `--target local` and `--target droplet` execute
the same phases through the same indirection, so what you validate on your
laptop is literally the code that runs on the box.

**What it produces:** the elapsed time from slow-shutdown to the first
successful authenticated query on 8.4. That is the number this runbook sizes
the window from, and it does not exist until step 7 below.

⚠ **The local run's duration is not the window.** Synthetic data, different
disk, a container rather than a droplet. `--target local` validates the
harness; only `--target droplet` against production-derived data produces a
number anyone may quote.

**Before you start,** on your laptop: `doctl` authenticated, this repo checked
out, and a recent nightly dump (`pfv2_<date>.sql.gz`).

**1. Render the config.** The script needs the *rendered* `my.cnf`, not the
jinja template, so it tests what actually lands on the box:

```bash
python3 - infra/ansible/roles/mysql/templates/my.cnf.j2 \
         infra/ansible/roles/mysql/defaults/main.yml > /tmp/rendered.cnf <<'EOF'
import re, sys
tpl = open(sys.argv[1]).read()
dv = dict(re.findall(r'^(mysql_\w+):\s*(\S+)\s*$', open(sys.argv[2]).read(), re.M))
tpl = re.sub(r'\{#.*?#\}', '', tpl, flags=re.S)
missing = [k for k in re.findall(r'\{\{\s*(\w+)\s*\}\}', tpl) if k not in dv]
assert not missing, f"unrendered vars: {missing}"
print(re.sub(r'\{\{\s*(\w+)\s*\}\}', lambda m: dv[m.group(1)], tpl))
EOF
```

⚠ If your inventory overrides any `mysql_*` var, render from the **inventory**,
not from `defaults/main.yml`, or you are validating a config the box will never
see.

**2. Validate the harness locally — do this before you spend a droplet.**

```bash
bash infra/rehearse-84-scratch-droplet.sh --target local --cfg /tmp/rendered.cnf
```

Expect `ALL GATES PASSED (local)` and exit 0. Roughly 5-10 minutes; it installs
Ubuntu's `mysql-server-8.0`, then performs the real dpkg swap to Oracle's
`mysql-community-server`. If this fails, the droplet run will fail the same way
and more expensively.

**3. Take the image.** Reuse step 0's backup, or snapshot production. Record the
id and the verb (`restore` for a backup, `rebuild` for a snapshot).

**4. Build the scratch droplet from that image** — same size and region as
`<data-droplet>`, so the CPU-derived 8.4 defaults resolve the way they will in
production:

```bash
doctl compute droplet create tbd360-rehearsal \
  --image <snapshot-or-backup-id> --size <same-size-as-data-droplet> \
  --region <same-region> --ssh-keys <your-key-id> --wait
doctl compute droplet get tbd360-rehearsal --format PublicIPv4 --no-header
```

⚠ Give it a **public** IP and keep it out of the production VPC. A scratch box
inside the VPC with production's private address is an availability hazard, not
a rehearsal.

**5. Record production's DD version**, so the rehearsal can prove it started
from the same place (operator-authorized read):

```sql
SELECT properties FROM mysql.dd_properties;   -- read DD_VERSION=NNNNN
```

**6. Put the dump on the scratch droplet.**

```bash
scp pfv2_<date>.sql.gz root@<scratch-ip>:/root/
```

**7. Run it.**

```bash
bash infra/rehearse-84-scratch-droplet.sh \
  --target droplet --host <scratch-ip> \
  --dump /root/pfv2_<date>.sql.gz \
  --cfg /tmp/rendered.cnf \
  --prod-dd <DD_VERSION from step 5>
```

**8. Read the result.** Exit code is the number of failed gates. Phase 10 prints
the elapsed DD-upgrade time — **record it in this file's evidence table**, and
size the window as that plus the App Platform scale-down/up and the cold
snapshot.

**9. Destroy the scratch droplet.** It holds a full copy of production data.

```bash
doctl compute droplet delete tbd360-rehearsal --force
```

#### If a phase fails

| Phase | Meaning | Stop? |
|---|---|---|
| 0 | Rendered config rejected by 8.0 or 8.4 | **Yes** — fix before anything else |
| 1 | Ubuntu 8.0 never came up on the snapshot | **Yes** — the snapshot is not what you think |
| 3 | `DD_VERSION` != production's | **Yes** — not a faithful rehearsal; the timing would be meaningless |
| 5 | Slow shutdown did not complete | **Yes** — the DD upgrade would start from a non-clean state |
| 6 | **Package swap failed** | **Yes** — this is the whole point; triage `/tmp/swap.log` on the box |
| 7 | Schema inventory changed, or a constraint stopped being enforced | **Yes** |
| 8 | A named variable moved | **Yes** — Oracle's packaging dropped the `/etc/mysql/mysql.conf.d` include |
| 9 | `debian.cnf` gone | No, but **schedule the logrotate fix** — it fails silently, days later |

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
   ⚠ **Use `mysql-apt-config`; do NOT hand-roll the sources list or the key.**
   The package embeds Oracle's signing key in its postinst and installs it to
   `/usr/share/keyrings/mysql-apt-config.gpg` with `signed-by=`. Measured
   2026-08-18 against `mysql-apt-config 0.8.39-1`: key `B7B3B788A8D3785C`,
   **valid to 2027-10-23**, repo usable, candidate `8.4.11-1ubuntu24.04`.
   ⚠⚠ **The standalone key file is a trap.** `RPM-GPG-KEY-mysql-2023` on
   `repo.mysql.com` **expired 2025-10-22**. Add it by hand and apt rejects the
   whole repo with `EXPKEYSIG B7B3B788A8D3785C ... is not signed`, which then
   surfaces as **`E: Unable to locate package mysql-community-server`** — it
   reads like a wrong component name and is not. Oracle re-issued the same key
   id as `RPM-GPG-KEY-mysql-2025`. Note that dev.mysql.com's quick guide still
   documents the manual route as `apt-key adv --recv-keys A8D3785C`, which can
   resolve the **stale** key from a keyserver. The release package is the safe
   path, which is why this step says to use it.
   ⚠ `mysql-apt-config` declares **`Pre-Depends: debconf, dpkg, lsb-release,
   wget, bash, gnupg`**. `dpkg -i` does **not** resolve pre-dependencies, so a
   missing one aborts the install — and the error names only the **first**
   missing package, so discovering them one at a time costs a run each. Install
   all six first. It is also interactive (debconf): preseed it rather than
   answering a TUI mid-window. `infra/rehearse-84-scratch-droplet.sh` carries
   the exact selections.
   ⚠ **Verify before the window, not during it.** `apt-get update` exits **0**
   against a repo whose signature failed, so its exit status is not evidence:

   ```bash
   apt-cache policy mysql-community-server   # must show a Candidate, not (none)
   ```

   ⚠ **amd64 only.** `repo.mysql.com` publishes `Architectures: i386 amd64` for
   Ubuntu — there is no arm64. The data droplet is amd64, so this is fine; an
   ARM droplet would make this cutover path impossible.
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
`innodb_buffer_pool_size`, `innodb_io_capacity`, `innodb_io_capacity_max`, and
**`innodb_redo_log_capacity`** (must be 268435456; if the include is dropped it
silently falls to 8.4's 100MB default — a 2.5x redo shrink that surfaces as
aggressive background flushing, not as an error).

**Resolved by measurement, not by blanket pinning.** The spec asked to pin
"~18 InnoDB values 8.4 re-defaults". Booting the same datadir under both
versions and diffing `SHOW GLOBAL VARIABLES` shows **exactly 10** change, and
most of them *reduce* memory on this box — `innodb_adaptive_hash_index` ON→OFF
frees buffer-pool memory, `innodb_change_buffering` all→none frees more,
`innodb_purge_threads` 4→1 is fewer threads on a 1-vCPU box. Pinning those back
would make the upgrade *worse*.

Two changes were acted on, both in `my.cnf.j2` with the measurement inline:

* **`innodb_doublewrite_pages` 4 → 128** is the only knob that increases
  resource use (32x the doublewrite buffer), and it is **deliberately NOT
  pinned** — 128 is a vendor performance fix (4 causes excessive fsyncs; 128
  measured elsewhere at ~55% fewer write IOPS), which is the right direction on
  an IOPS-limited droplet, and it costs disk rather than RAM. The reasoning is
  inline in `my.cnf.j2`.
  ⚠ **So expect `128` after the cutover, and do not read it as a dropped
  config include.** An earlier revision of this runbook claimed the value was
  "pinned to 4"; it never was, and it is set nowhere in the Ansible tree.
  Section 5 has you diff `SHOW GLOBAL VARIABLES` precisely to detect a dropped
  include, so a stale expectation here manufactures a false alarm at the worst
  possible moment — mid-window, after the point of no return.
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
- ⚠⚠ **If you also run Phase 2 (the `pfv2` → `tbd` rename), `DATABASE_URL` is
  bound in `.do/app.yaml` TWICE** — once on the `backend` service and once on
  the `migrate` PRE_DEPLOY job — as two separately-encrypted `EV[]` values.
  Both name the database in the URL, so **both must be re-encrypted** after the
  rename. Miss the job's copy and nothing breaks at rename time: the backend
  comes up green and the failure lands at the **next deploy**, when the migrate
  job cannot reach `pfv2` any more. Verified live on deployment
  `2026-08-17`: the job logs `{"database": "pfv2", "event": "migrate.no_op"}`,
  so it is genuinely reading its own binding, not inheriting the service's.
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
