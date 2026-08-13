# MySQL 8.0 → 8.4 cutover runbook (TBD-360)

The repo-side pre-flight is **done and verified**. What remains is the part that
touches the live droplet. Everything below is an operator action: the agent
loop does not write to the production database and does not run the cutover.

Spec: `specs/2026-08-09-mysql-84-lts-upgrade.md`. This file is the short,
measured version of what to actually type.

---

## What is already proven, and how

| Claim | How it was verified |
|---|---|
| 8.4 refuses to start on the current config | `mysqld --validate-config` on real `mysql:8.4`: exit 1, `unknown variable 'default-authentication-plugin=mysql_native_password'` |
| Removing that line is rollback-safe | Same check on `mysql:8.0` with the fixed config: exit 0. So the fix can land on the 8.0 box, which must still start |
| `mysql_native_password` is unusable on 8.4 | `PLUGIN_STATUS` = `DISABLED`; `CREATE USER ... IDENTIFIED WITH mysql_native_password` → `ERROR 1524 Plugin ... is not loaded` |
| Accounts convert in place on 8.0 and still authenticate | `ALTER USER ... IDENTIFIED WITH caching_sha2_password BY '<pw>'` on real 8.0, then login OK |
| The app works on 8.4 | Full stack on `mysql:8.4.11`: `/ready` → 200 `database: connected`, all 79 alembic revisions to head, idempotent re-run, `utf8mb4_0900_ai_ci` preserved |
| The driver stack works | `cryptography 44.0.3` confirmed **in the image**, `aiomysql 0.2.0`, `PyMySQL 1.1.3`; app authenticates with `caching_sha2_password` over non-TLS |
| Nothing regressed | Full backend suite against real 8.4: **4106 passed, 12 skipped, 1 xfailed, 0 failed** — identical to the 8.0 baseline |

⚠ **The one thing that would have taken production down.** Flipping
`plugin: mysql_native_password` → `caching_sha2_password` in the Ansible role
is NOT sufficient, and fails silently. `community.mysql.mysql_user` **ignores
`password:` when `plugin:` is set** — it writes the plugin and leaves
`authentication_string` EMPTY, while reporting `changed: true` and succeeding.
Measured on a real 8.0 with the real collection:

```
plugin + password:            plugin=caching_sha2_password, hash_len=0   -> ERROR 1045 Access denied
plugin + plugin_auth_string:  plugin=caching_sha2_password, hash_len=70  -> login OK
```

The role now uses `plugin_auth_string`. Do not "simplify" it back.

---

## Order of operations

### 0. Land TBD-399 FIRST

`enable_backups = false` in `infra/terraform/main.tf` overrides the module's own
`default = true`. Until that is flipped, there is no scheduled snapshot to fall
back to and the "one-way door" label is real. With backups on, a cold snapshot
plus in-place `restore-droplet` restores the whole 8.0 datadir **and preserves
the private IPv4 that App Platform is pinned to**.

**Do not run the cutover before TBD-399 is applied.**

### 1. Apply the repo changes to the 8.0 box (non-destructive)

This PR changes `my.cnf.j2` (drops the fatal variable) and the three account
definitions. Running the play on the CURRENT 8.0 server:

- re-templates `my.cnf.j2` and triggers `notify: Restart mysql` — safe, 8.0
  accepts the fixed file (verified, exit 0);
- converts `pfv_app@%`, `pfv_app@localhost` and `pfv_backup@localhost` to
  `caching_sha2_password` **on 8.0**, where they keep working.

After the play, verify by hand on the droplet:

```sql
SELECT user, host, plugin, LENGTH(authentication_string) AS hash_len
FROM mysql.user WHERE user IN ('pfv_app','pfv_backup') ORDER BY user, host;
```

⚠ **`hash_len` must be 70 for all three rows.** A `0` means the passwordless
form got in; fix it before going any further, or the app is already down.

Then prove the app and the backup path still work on 8.0:

```bash
mysql -u pfv_app -p -h 127.0.0.1 -e "SELECT 1;"
mysqldump -u pfv_backup -p --single-transaction --databases pfv2 | head -3
```

⚠ Note the play is **no longer idempotent** on those three tasks
(`plugin_auth_string` is cleartext, so it rewrites every run and always reports
`changed`). That is expected. It is not a signal that something differed.

### 2. Test-restore a real dump into a throwaway 8.4 instance

An untested restore is not a rollback plan, and it is the only rollback
available besides the snapshot.

```bash
# on a scratch machine, not the data droplet
docker run -d --name restore-probe -e MYSQL_ROOT_PASSWORD=... mysql:8.4
docker exec -i restore-probe mysql -uroot -p... < production-dump.sql
docker exec restore-probe mysql -uroot -p... -e "SELECT COUNT(*) FROM pfv2.transactions;"
```

Compare row counts against production before trusting it.

### 3. Snapshot immediately before the window

Cold snapshot (droplet powered off) is the one that restores cleanly.

### 4. Cutover

Per the spec: replace Ubuntu's `mysql-server-8.0` with Oracle's
`mysql-community-server` via `mysql-apt-config`. Consequences to handle,
all named in the spec: no `debian-sys-maint` / `/etc/mysql/debian.cnf`
(Ubuntu's logrotate authenticates with it), different config layout, different
AppArmor profile, different systemd unit, and unattended-upgrades needs a hold
so it cannot reinstall 8.0 over the top.

Run `mysqld --validate-config` with the **8.4 binary against the final config**
before stopping 8.0.

### 5. Resource check on this specific box

8.4 changes ~20 InnoDB defaults. `innodb_io_capacity` moves 200 → 10000, which
on an `s-1vcpu-2gb` droplet **co-hosting Redis** over-issues background flush
I/O and is a plausible OOM path for the co-resident Redis. `my.cnf.j2` already
pins `innodb_io_capacity` / `innodb_io_capacity_max` explicitly, so 8.4's
defaults do not apply — keep it that way. Capture a
`SHOW GLOBAL VARIABLES` diff before/after anyway.

### 6. After

- `/ready` green, one real authenticated request served
- run the backup script by hand and confirm a non-empty dump
- ⚠ the nightly backup has four known holes (on-disk only, `pfv2` only so
  **grants are not backed up**, `gzip >` creates the file before `mysqldump`
  fails so existence ≠ success, no alerting) → **TBD-400**

---

## Rollback

1. Restore the pre-cutover snapshot in place (preserves the private IPv4).
2. Failing that, rebuild 8.0 and restore the tested dump.

In-place downgrade from 8.4 to 8.0 is **not supported**. Do not try it.
