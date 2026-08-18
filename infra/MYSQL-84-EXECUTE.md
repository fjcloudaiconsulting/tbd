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
$SSHQ 'mysql -N -B -e "SELECT CURRENT_USER()"'   # NO --no-defaults, on purpose
terraform -chdir=$TFDIR output -json | python3 -c 'import json,sys; d=json.load(sys.stdin); print("TF secrets present:", all((d.get(k) or {}).get("value") for k in ("mysql_app_password","mysql_backup_password","redis_password")))'
doctl compute droplet backups $DROPLET_ID --format Name,Created --no-header | head -3
```

✅ EXPECT: `SSH_OK`, `8.0.46-...`, `TF secrets present: True`, and **at least one
backup row**.

The second line is the one that is easy to skim past. It is the *only* step in
this sheet that runs a bare `mysql` before the one-way door — everything else
uses `--no-defaults`, which by design cannot see the problem.

✅ EXPECT **before** Phase 1: `pfv_backup@localhost`. That is the known live
footgun, and Phase 1 fixes it. ✅ EXPECT **after** Phase 1: `root@localhost`.

⚠ Anything else — a third user entirely — means a `[client]` section in a file
the play does not manage (`/etc/mysql/my.cnf`, `/root/.mylogin.cnf`). Phase 1's
own fence would catch it, but it would catch it *mid-play*, after the
credentials have rotated and before the redis role has run. Find it now.

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
( umask 077; ./infra/ansible/bin/run-playbook.sh --production --check --diff > /tmp/tbd360-dryrun.txt 2>&1 )
grep -E "PLAY RECAP|^pfv-data-01" /tmp/tbd360-dryrun.txt
less /tmp/tbd360-dryrun.txt      # read the diff, then:  rm -f /tmp/tbd360-dryrun.txt
```

⚠⚠ **That file contains two production secrets in cleartext.** `--check --diff`
renders the template diffs, and neither secret-bearing task is `no_log`: the
rotated `mysql_backup_password` (`roles/mysql/templates/root.my.cnf.j2`) and the
rotated `redis_password` (`roles/redis/templates/00-static.conf.j2`) are both in
the payload you are being asked to read. Hence the subshell `umask 077`, and
hence `rm -f` when you are done. `run-playbook.sh` goes to some trouble to keep
these off disk (mode-0600 mktemp, trapped on every exit path); do not undo that
with a redirect.

✅ EXPECT: `failed=0`. Some `changed` is normal — the three user tasks rewrite
every run by design (`plugin_auth_string` is cleartext and cannot be compared).

⚠ The play's verification fences are **skipped under `--check`**, deliberately.
They assert properties of the *converged* server, and `--check` converges
nothing: under `--check` the `command` module self-reports `skipped` with
`stdout` left at `''` rather than running the query, so the reads come back
empty and the asserts fail on a healthy box. Measured against production
2026-08-18 — the dry run reported `failed=1` on
`Assert a bare mysql run as root IS root`, whose fail message named the
authenticated user as `"nothing"`: an empty read, not a real identity. They are
gated on `not ansible_check_mode`; a real run still asserts.

**Read the diff, do not just count the failures.** Measured against production
2026-08-18 **on the gated play** — the `failed=1` observation above predates the
gating and came from a run that never reached the redis and backups roles.
`--check --diff` reported `changed=10`, and every one was expected:

| Changed | Meaning |
|---|---|
| `Update apt cache and upgrade packages` | 27 upgrades + 8 new, kernel included. No mysql/redis packages **on the day this was measured — re-check, it is not a property of the role.** See 0.4 |
| `Set system timezone` | `Etc/UTC` → `UTC`; cosmetic, rewrites every run |
| `Create backup user` + 2 × `Create application MySQL user` | the password rotation + `caching_sha2_password` conversion |
| `Drop /root/.my.cnf` | removes the `[client]` section — the footgun is **still live on production** |
| `Drop pfv MySQL config override` | drops `default-authentication-plugin`, and restates `innodb_log_file_size 128M` as `innodb_redo_log_capacity 256M` (**same 268435456 bytes** — 128M × the default `innodb_log_files_in_group=2`; a restatement, not a resize). **Notifies a MySQL restart** |
| `Drop pfv Redis static config override` | `requirepass` rotation. **Notifies a Redis restart.** ⚠ Confirm the diff touches *only* that line — the same file carries `bind 127.0.0.1 <private_ipv4>`, and a change there means Redis stops listening on the address App Platform uses |
| 2 handlers | the two restarts above |

⚠⚠ **Phase 1 therefore restarts BOTH MySQL and Redis while the app is still
serving.** The sheet quiesces in Phase 2, not Phase 1. Sessions survive the
Redis restart (AOF is on, deliberately — Redis is the auth-session store), but
every client connection is dropped and the admin dashboard reports
`Redis: DOWN` until the pool reconnects — *and* `requirepass` has rotated, so it
cannot reconnect at all until `REDIS_URL` is updated in 1.2.

If a visible blip matters, you may scale the backend to zero (2.1) *before*
running Phase 1 — but ⚠⚠ **scale it back to 1 for the 1.2 `/ready` check, then
back to zero.** Do not defer that check to 4.3. It is the only proof that both
`DATABASE_URL` bindings took, and 4.3 is on the far side of Phase 3's one-way
door: a wrong password on the `migrate` job discovered there costs a snapshot
rebuild and a full re-run of Phase 1, where discovered at 1.2 it costs an edit.

### 0.4 Take the OS package upgrade OUTSIDE the window

The `common` role runs `apt upgrade: safe` unconditionally, so a stale box drags
an unbounded apt run — kernel included — into the middle of Phase 1. Doing it in
advance makes Phase 1 purely a credentials-and-config step.

⚠ **This step is itself a short blip, not a free one.** Budget a few minutes at
a quiet moment; it ends in a deliberate reboot (see below).

**First, prove it is safe to do outside the window.** This only simulates:

```bash
$SSHQ 'apt-get update -qq && apt-get -s upgrade --with-new-pkgs 2>/dev/null | grep -Ei "^(Inst|Conf) .*(mysql|redis|libmysql)" || echo "  no mysql/redis packages in the upgrade (good)"'
```

✅ EXPECT the `good` line. ⚠ **If anything matches, stop.** Upgrading MySQL or
Redis here restarts production's database with the backend still serving, before
the Phase 2 snapshot exists and without Phase 3.1's slow-shutdown discipline.
That work belongs inside the window, after the snapshot.

```bash
$SSHQ 'export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
apt-get update -qq
apt-get -y --with-new-pkgs -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold upgrade
echo "apt-exit=$?"'
```

✅ EXPECT `apt-exit=0` and no `kept back` line.

⚠ `--with-new-pkgs` is what makes this equivalent to the role's `upgrade: safe`
(`ansible.builtin.apt` maps `safe` to `apt-get upgrade --with-new-pkgs`).
Without it, apt holds back every package that needs a *new* one installed —
which is exactly the 8 new kernel packages — and 0.3 keeps reporting the apt
task as `changed` no matter how many times you run this.

⚠ `export`, not a `VAR=x cmd` prefix: an assignment prefix binds to the single
command it precedes, so the old form set the frontend on `apt-get update` (which
never prompts) and not on the upgrade (which can). `$SSHQ` carries
`-o BatchMode=yes` and no tty, so a debconf prompt there is a hang holding the
dpkg lock, not a question.

⚠ Do not pipe this to `tail`. The `kept back` list, the package names, and any
`dpkg: error processing` all scroll past in the middle, and a pipeline's exit
status is the *last* command's, so `$?` would be `tail`'s and always 0.

**Then reboot, deliberately, here rather than inside the window:**

```bash
$SSHQ 'ls /var/run/reboot-required 2>/dev/null && (systemctl reboot &) ; echo requested'
sleep 60; $SSHQ 'uptime; uname -r; systemctl is-active mysql redis-server'
```

✅ EXPECT the box back, the new kernel in `uname -r`, and both services
`active`.

⚠ Rebooting now is the point, not a side effect. `NEEDRESTART_MODE=l` above
tells needrestart to *list* services rather than restart them — a library
upgrade (libssl, libc, libkrb5) can otherwise restart mysqld and redis-server
even though no mysql or redis *package* was upgraded, which is a surprise
restart of production's database in a step labelled "no outage". The reboot
replaces that with one you chose. It also means Phase 2 snapshots a system whose
new kernel has already been proven to boot: deferring the first boot to the
power-cycle in 2.2 puts it inside the outage, on the machine that then has to
survive an irreversible package swap, and makes your primary undo a snapshot of
an unproven kernel.

**Now re-run 0.3.** The sheet is copy/paste in order, and this is the step that
makes 0.4 verifiable:

```bash
( umask 077; ./infra/ansible/bin/run-playbook.sh --production --check --diff > /tmp/tbd360-dryrun.txt 2>&1 )
grep -E "PLAY RECAP|^pfv-data-01" /tmp/tbd360-dryrun.txt; rm -f /tmp/tbd360-dryrun.txt
```

✅ EXPECT `failed=0` and **`changed=9`**, not the `changed=10` in the table
above: the apt row is gone and the other nine are unchanged. That drop from 10
to 9 *is* the evidence that 0.4 worked.

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

⚠⚠ **Keep `--no-defaults`, even though the root cause is now fixed.**

The `[client]` section was removed from `/root/.my.cnf` (see
`roles/mysql/templates/root.my.cnf.j2`), and Phase 1's play asserts that a bare
`mysql` run as root really is `root@localhost`. So on a freshly-played box this
flag is belt-and-braces.

It stays in this sheet for three reasons, each of which is live during a window:

1. **Production has not been played yet** when you start. The old
   `/root/.my.cnf` with its `[client]` section is still there until Phase 1
   completes.
2. **A snapshot rollback restores the old file.** If you fall back to the Phase
   2 snapshot, the trap comes back with it.
3. The template only governs `/root/.my.cnf`. A `[client]` section in
   `/etc/mysql/my.cnf` or `~/.mylogin.cnf` would do the same thing, and nothing
   rewrites those.

Without it, a bare `mysql` as root authenticates as the low-privilege backup
user and this step fails with `ERROR 1227` — then dpkg stops mysqld with the
*default fast* shutdown and the DD upgrade starts from a non-clean state. It
looks survivable, and it is exactly the failure this step exists to prevent.

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
