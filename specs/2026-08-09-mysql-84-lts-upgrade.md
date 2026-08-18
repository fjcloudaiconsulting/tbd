> ## ⚠ SUPERSEDED IN PART — read `infra/MYSQL-84-CUTOVER.md` first
>
> This document is the original **analysis** and is kept for its reasoning. Six
> of its conclusions were overturned by measurement between 2026-08-09 and
> 2026-08-18. Do not execute from this file.
>
> | This spec says | Measured reality |
> |---|---|
> | Pin "~18 InnoDB values 8.4 re-defaults" | Only **10** change, and most *reduce* memory on a 1-vCPU box. Pinning them back would make the upgrade worse. Two were acted on; `innodb_doublewrite_pages` is deliberately **not** pinned |
> | Nothing about client defaults | `/root/.my.cnf` forces `user = pfv_backup`, so step 10's `SET GLOBAL innodb_fast_shutdown = 0` **fails with `ERROR 1227`** unless every `mysql` call uses `--no-defaults` |
> | Add the repo by hand | The standalone `RPM-GPG-KEY-mysql-2023` **expired 2025-10-22**; apt then rejects the repo and it surfaces as `E: Unable to locate package mysql-community-server`. Use the `mysql-apt-config` package, which ships the renewed key |
> | Hand-run `ALTER USER` for the conversion | The Ansible play does it, driven from Terraform-generated credentials (TBD-206/TBD-207) |
> | Duration unknown | **49 seconds**, measured end to end on a real droplet. Production is 6.7 MB across 50 tables |
> | Rename needs its own window | Folded in as Phase 2 — verified metadata-only (0 views, 0 triggers, 0 routines, 50 tables) |
>
> **Execution sheet:** `infra/MYSQL-84-EXECUTE.md`.

---
name: MySQL 8.0 -> 8.4 LTS upgrade (<data-droplet>)
description: Runbook and risk analysis for moving the self-hosted production data plane off end-of-life MySQL 8.0
type: project
---

# MySQL 8.0 -> 8.4 LTS upgrade

**Status:** spec, not yet scheduled. Execution is a Sprint 9 ticket requiring explicit operator authorization — this is a production database with no replica.
**Target:** MySQL **8.4 LTS**. Operator decision, 2026-08-09.
**Scope:** `<data-droplet>` (production), plus the dev and CI pins that must move with it.

---

## Why

MySQL 8.0 Premier Support ended 2025-04-30; Extended Support ended 2026-04-30. It is now under Oracle "Sustaining Support", which continues indefinitely but ships **no new fixes, no security alerts, and no Critical Patch Updates**. For a finance application holding user financial records that is a security posture problem, not a version-hygiene one.

8.4 LTS: Premier to 2029-04-30, Extended to 2032-04-30.

### Open question, deliberately not closed here

**MySQL 9.7 is also an LTS** (GA 2026-04-21, supported to ~2034), and Oracle's EOL notice recommends "8.4 LTS **or** 9.7 LTS". This does **not** change the immediate work: the documented upgrade rules state *"a bugfix or LTS series cannot be skipped"*, so 8.0 -> 8.4 is mandatory regardless of the eventual destination. Whether to make a later 8.4 -> 9.7 hop (a supported next-LTS step, buying ~2 extra years) is a separate decision to take **after** this lands. Note 9.0 **removes** `mysql_native_password` entirely, so the auth work in this spec is a prerequisite for that hop either way.

An earlier framing of this plan asserted that 9.x is Innovation-track-only and therefore unsuitable. That was true of 9.0–9.6 and became stale in April 2026. Recorded so it is not repeated.

---

## The blocker: the server will refuse to start

`infra/ansible/roles/mysql/templates/my.cnf.j2:7` contains:

```
default-authentication-plugin = mysql_native_password
```

`default_authentication_plugin` was deprecated in 8.0.27 and **removed in 8.4.0**. MySQL treats an unknown variable in an option file as fatal: the server displays a diagnostic and **exits**, it does not warn and continue. Starting 8.4 against the current config therefore takes the data plane down with `unknown variable 'default_authentication_plugin=mysql_native_password'`.

**This is the single highest-risk item in the plan, and it is also the cheapest to de-risk.** `mysqld --validate-config` is a documented dry-run that parses the config and exits 0 (clean) or 1 (error) without starting the server. Run it with the 8.4 binary against the live config **before** stopping the 8.0 server.

Obsolete `sql_mode` values have the same fail-to-start behaviour and are caught by the same check.

### Other removed variables to check for

Not currently in our `my.cnf`, but verify with `--validate-config` rather than by reading:
`expire_logs_days`, `skip-host-cache`, `character-set-client-handshake`, `innodb`/`skip-innodb`, `ssl`, `have_ssl`/`have_openssl`, the four `master-info-*`/`relay-log-info-*` variables, `transaction_write_set_extraction`, `binlog_transaction_dependency_tracking`, `log_bin_use_v1_events`, `slave-rows-search-algorithms`, `avoid_temporal_upgrade`, `show_old_temporals`, `old`/`new`, `old-style-user-limits`, `no-dd-upgrade`, `language`.

---

## Authentication: three accounts must move first

`mysql_native_password` is **disabled by default in 8.4** (re-enablable with `mysql_native_password=ON` in `[mysqld]`) and **removed in 9.0**. Four sites depend on it:

| Site | What it is |
|---|---|
| `roles/mysql/tasks/main.yml:37` | backup user — the account the nightly cron `mysqldump` runs as |
| `roles/mysql/tasks/main.yml:72` | account |
| `roles/mysql/tasks/main.yml:92` | account |
| `roles/mysql/templates/my.cnf.j2:7` | server default (the fatal one above) |

**Convert to `caching_sha2_password` on 8.0, before the upgrade.** Doing it ahead of time means the `mysql_native_password=ON` escape hatch is never needed, and it is a hard prerequisite for any later 9.7 hop.

```sql
SELECT user, host, plugin FROM mysql.user WHERE plugin <> 'caching_sha2_password';
ALTER USER '<u>'@'<h>' IDENTIFIED WITH caching_sha2_password BY '<pw>';
```

Do not miss non-app accounts: backup, monitoring, and `root@localhost` (which stays on `auth_socket` deliberately — see the role's comment; leave it alone).

In 8.4 the `authentication_policy` default is `'*,,'`, meaning factor 1 is `caching_sha2_password`. So the removed line should simply be **deleted**, not translated.

### The driver side — verified, with one live hazard

The app reaches MySQL over the private VPC with **no TLS** (`backend/app/database.py:13-14`, explicit). Under `caching_sha2_password` on a plaintext connection the client must obtain the server's RSA public key.

- There is **no server-side `allow-public-key-retrieval`** setting — that name is a Connector/J concept, not MySQL server. The only server requirement is that the RSA keypair exists, and `caching_sha2_password_auto_generate_rsa_keys` defaults ON, so the existing datadir already has `private_key.pem` / `public_key.pem`.
- The server does not volunteer the key; the client must request it. **aiomysql requests it unconditionally and automatically.**
- `cryptography` is a **hard requirement** — PyMySQL raises `RuntimeError("'cryptography' package is required for sha256_password or caching_sha2_password auth methods")`. We pin `cryptography==44.0.3` in `backend/requirements.txt:26`, so this is satisfied — **but verify it is present in the deployed image, not just the requirements file.**

⚠ **The failure mode is total, not gradual.** The `caching_sha2_password` fast-auth cache is empty after every server restart, so a missing `cryptography` wheel fails **100% of connections immediately** post-upgrade. This is the same shape as the 2026-05-20 aiomysql outage.

⚠ **Do not bump `aiomysql==0.2.0` or `PyMySQL==1.1.3` as part of this work.** `requirements.txt:7-14` documents that SQLAlchemy's mysql dialect inspects PyMySQL's `connect.__doc__`, and a newer PyMySQL breaks `ping()`. Version changes belong in their own ticket with their own fence.

---

## The package swap is not a version bump

Ubuntu 24.04's archive ships MySQL **8.0.46** and will never offer 8.4. `roles/mysql/tasks/main.yml` installs plain `mysql-server` from the distro repo. Moving to 8.4 means replacing Ubuntu's `mysql-server-8.0` with **Oracle's `mysql-community-server`** via `mysql-apt-config` (select the `mysql-8.4-lts` series).

That swap changes more than the version, and each item below is a real breakage path on this box:

- **Config layout** differs (`/etc/mysql/mysql.conf.d/` vs `/etc/my.cnf`). Our template lands in the Ubuntu path; confirm the Oracle package still includes it.
- **No `debian-sys-maint` account and no `/etc/mysql/debian.cnf`.** Ubuntu's `/etc/logrotate.d/mysql-server` authenticates with that credential. Audit every cron, logrotate and monitoring script for it before cutover.
- **AppArmor profile** differs and can silently block a non-default `datadir`.
- **systemd unit** differs.
- **unattended-upgrades** could reinstall 8.0 over the top — pin/hold the Ubuntu packages.

---

## 8.4 changes ~20 InnoDB defaults, and some are hostile to this box

`<data-droplet>` is an `<droplet-size>` droplet **co-hosting Redis/Valkey**. Notable default changes: `innodb_adaptive_hash_index` ON->OFF, `innodb_change_buffering` all->none, `innodb_doublewrite_pages`->128, plus changes to `innodb_page_cleaners`, `innodb_parallel_read_threads`, `innodb_purge_threads`, `innodb_read_io_threads`, `innodb_log_buffer_size` and the `temptable_*` family.

The one that matters most here: **`innodb_io_capacity` default moves 200 -> 10000.** On DO block storage that over-issues background flush I/O, and the extra threads plus larger log buffer raise baseline RSS — a plausible OOM path for the co-resident Redis on a 2 GB box.

Our `my.cnf` already pins `innodb_io_capacity` and `innodb_io_capacity_max` from variables, which protects us on those two specifically. **Pin the rest explicitly rather than inheriting 8.4's defaults**, and capture a `SHOW GLOBAL VARIABLES` diff before and after.

---

## Backups, and why rollback is the weak point

**In-place downgrade from 8.4 is not supported.** The documented downgrade path is logical dump-and-load only, and only "for rollback purposes … if no new server functionality has been applied to the data." So rollback means restore-from-dump or a droplet snapshot. There is no replica to fail over to.

Backup-script exposure (`roles/backups/templates/mysql-backup.sh.j2`):

- The script's `mysqldump` invocation uses `--single-transaction --routines --triggers --quick --hex-blob` — all still valid in 8.4.
- It authenticates as the backup user via `/root/.my.cnf` — **that account is one of the three that must be converted** (above), or every nightly backup fails silently after cutover.
- `mysqlpump` is **removed** in 8.4; `--master-data` is now a deprecated alias for `--source-data`. Neither appears in our script, but re-check before the window.
- A stored dump containing removed replication SQL (`CHANGE MASTER TO`, `RESET MASTER`, `START SLAVE`, `SHOW MASTER STATUS`) will not replay into 8.4.

⚠ **An untested restore is not a rollback plan, and per the above it is the only rollback we have.** Test-restoring a current dump into a throwaway 8.4 instance is a required pre-flight step, not an optional one.

---

## The verification we now have for free

TBD-212 shipped a `Migration Checks` CI job that runs all 80 alembic revisions from empty against a real MySQL service container, then boots the app and asserts `/ready`. **Flipping that job's image from `mysql:8.0` to `mysql:8.4` exercises the entire schema and the async driver against the target version, in CI, before anything touches production.**

The fact-check flagged one genuinely unverified item: aiomysql 0.2.0 predates 8.4's GA by ~10 months and is lightly maintained. No documented incompatibility was found, but *"the async driver works against 8.4"* should be treated as unverified until the backend suite runs against a real 8.4 container. That is cheap, and it is the highest-value single verification in this plan.

---

## Pre-flight (all non-destructive, days ahead of the window)

1. Flip CI `Migration Checks` to `mysql:8.4`; confirm 80 revisions + `/ready` green. Flip `docker-compose.yml` to `mysql:8.4`; run the full backend suite.
2. Take a droplet snapshot.
3. Take a fresh logical dump; **test-restore it into a throwaway 8.4 instance** and confirm it replays clean.
4. Audit `mysql.user` for non-`caching_sha2_password` accounts; convert them on 8.0 and confirm the app and the nightly backup still work.
5. Run `mysqld --validate-config` with the 8.4 binary against the live `my.cnf`; expect exit 1 until the `default-authentication-plugin` line is removed, then exit 0.
6. Audit cron/logrotate/monitoring for `debian-sys-maint` / `/etc/mysql/debian.cnf`.
7. Capture `SHOW GLOBAL VARIABLES` as the before-baseline.

## Cutover

8. Announce the window. App Platform: scale backend to 0 (same lever `infra/MIGRATION.md` uses).
9. Final dump.
10. `SET GLOBAL innodb_fast_shutdown = 0;` then `mysqladmin shutdown`. **Slow shutdown, not 2** — required for in-place upgrade.
11. Swap packages to the Oracle 8.4 repo; hold the Ubuntu ones.
12. Start 8.4. The server performs the data-dictionary upgrade itself at startup — `mysql_upgrade` was removed in 8.4 and must not be invoked.
13. Verify: `SELECT VERSION()`, `/ready`, a real authenticated request, the nightly backup script by hand, and the `SHOW GLOBAL VARIABLES` diff.
14. Scale the backend back up.

## Rollback

Restore the droplet snapshot, or rebuild 8.0 and restore the final dump. **Decide and write down the go/no-go point** — once step 12 completes and writes land, dump-restore is the only way back and any writes taken on 8.4 are lost.

---

---

# Phase 2 — rename the database `pfv2` -> `tbd`

Operator addition, 2026-08-09. Folded into this ticket because it shares the maintenance window, and split out of **TBD-205** (which keeps the code/CLI/compose half).

⚠ **TBD-205 carries a decision-in-force that says the opposite**: *"This touches the database name, which makes it a migration and ops event rather than a rename. Sequence it when there is no other in-flight schema work."* That note assumed the rename would need its own window. The operator's call is that a planned window with a verified dump and a snapshot is a **better** home than a standalone event, not a worse one. Recorded so the override is visible rather than silent.

## This is a separate PHASE, not a merged step

Run it **after** the 8.4 upgrade is verified (step 13), never interleaved. Two reasons:

1. The upgrade is **in-place** and does not restore anything, so the rename is not "free from the dump" — it is free because it is metadata-only (below). Switching the upgrade to a logical dump-and-restore just to land the rename would trade a fast, documented path for a slow one and muddy the rollback.
2. If the app fails to come up, one variable at a time. The upgrade is effectively irreversible; **the rename is trivially reversible** (rename back). Keeping them sequential means a failure is immediately attributable.

## Why it is cheap here — verified, not assumed

- **No views, triggers, or stored routines exist.** `grep -riE "create (or replace )?(view|trigger|procedure|function)"` across `backend/alembic/versions/` returns nothing. Those objects do **not** move with `RENAME TABLE`, and their absence is what makes this a complete operation rather than a partial one. ⚠ Re-run that grep at execution time — a migration added between now and then invalidates this.
- MySQL has **no `RENAME DATABASE`**. `RENAME TABLE` across schemas is the documented approach and is metadata-only for InnoDB — no data copy, fast regardless of table size.
- `alembic_version` is an ordinary table and moves with the rest, so alembic continues from the same head with no stamping.

## ⚠ It must be ONE atomic statement

There are **83 FK declarations** across the models. `RENAME TABLE` executed per-table would leave foreign keys pointing at tables not yet moved, and fail partway with the schema half-renamed. MySQL renames all pairs in a single statement atomically:

```sql
CREATE DATABASE tbd CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;

RENAME TABLE
  pfv2.accounts       TO tbd.accounts,
  pfv2.alembic_version TO tbd.alembic_version,
  ...                                          -- ALL 50 tables, one statement
  pfv2.users          TO tbd.users;
```

Generate the statement rather than hand-typing it, from
`SELECT table_name FROM information_schema.tables WHERE table_schema='pfv2' AND table_type='BASE TABLE'`,
and **assert the generated list length equals the table count** before executing — a truncated list is a half-rename.

⚠ The new schema's charset/collation must match exactly (`utf8mb4` / `utf8mb4_0900_ai_ci`). Per-table collations are carried by the tables themselves, but the schema default governs anything created later — and this repo has a live production incident (TBD-322) rooted in collation semantics.

## Grants and the app user

The production app user is **`pfv_app`** (`infra/ansible/roles/mysql/defaults/main.yml:4`), *not* `pfv2` — `MYSQL_USER=pfv2` in `.env.example:19` is the dev/compose user only. Grants are schema-scoped, so `pfv_app` needs privileges on `tbd.*`; the old `pfv2.*` grant is revoked after verification.

**Renaming the database user is explicitly OUT of scope** — it is a separate, smaller change with its own credential-rotation blast radius, and it belongs with TBD-205's remaining half.

## Sequence (after upgrade step 13 has passed)

1. Confirm the app is healthy on 8.4 against `pfv2`. Do not proceed otherwise.
2. Scale backend to 0 again (writes must be quiesced — `RENAME TABLE` takes metadata locks).
3. `CREATE DATABASE tbd` with the charset/collation above.
4. Generate and length-assert the rename statement; execute it as one statement.
5. `GRANT` on `tbd.*` to `pfv_app`.
6. Update `DATABASE_URL` in App Platform. ⚠ `.do/app.yaml` is authoritative and its `EV[]` blobs **must** land in the repo — see `reference_do_spec_sync`.
7. Scale up; verify `/ready`, a real authenticated request, and `SELECT COUNT(*)` on 3-4 core tables against the pre-rename baseline.
8. Run the nightly backup script by hand — it targets `mysql_app_db`, which must have moved to `tbd`.
9. Only after all of the above: `DROP DATABASE pfv2` (now empty). **Leave it for at least one full backup cycle.**

## Rollback for this phase

Reverse `RENAME TABLE` (same atomic form, `tbd.* TO pfv2.*`), restore `DATABASE_URL`. Cheap and complete — which is exactly why it goes last.

## Repo changes Phase 2 adds

| File | Change |
|---|---|
| `infra/ansible/roles/mysql/defaults/main.yml:3` | `mysql_app_db: pfv2` -> `tbd` |
| `infra/ansible/inventory.yml.example:13` | same |
| `.env.example:18` | `MYSQL_DATABASE=pfv2` -> `tbd` |
| `.github/workflows/test.yml` | `MYSQL_DATABASE`/`DATABASE_URL` in the `migrations` job |
| `docker-compose.yml` | DB name in the mysql service env + `DATABASE_URL` |
| `k8s/values.yaml:40`, `k8s/Chart.yaml:2` | `pfv2` -> `tbd` |
| `.do/app.yaml` | `DATABASE_URL` (EV[] blob must land in repo) |

⚠ **Dev stacks keep their existing volume.** A developer's `mysql_data` volume still holds a `pfv2` schema; after the pin changes they get an empty `tbd` and migrations re-run from scratch. That is fine for dev but must be **called out in the PR**, or it reads as data loss. `./pfv reset` is the clean path.

## Still out of scope after Phase 2

The rest of TBD-205: the `./pfv` CLI name, compose service names, env prefixes, the repo directory, and the TFC workspace `<tfc-org>/<data-workspace>` (which cannot be renamed from the CLI — Terraform is VCS-driven with manual Confirm & Apply).

---

## Repo changes this ticket carries

| File | Change |
|---|---|
| `infra/ansible/roles/mysql/templates/my.cnf.j2` | delete the `default-authentication-plugin` line; pin InnoDB values that 8.4 re-defaults |
| `infra/ansible/roles/mysql/tasks/main.yml` | `plugin: caching_sha2_password` at the three account sites; add the Oracle APT repo + package hold |
| `.github/workflows/test.yml` | `mysql:8.0` -> `mysql:8.4` in the `migrations` job |
| `docker-compose.yml` | `mysql:8.0` -> `mysql:8.4` |
| `infra/MIGRATION.md` | add this runbook, or link it |
| Docs | `README.md`, `CLAUDE.md`, `DEPLOYMENT.md`, `infra/README.md`, `infra/terraform/README.md` all say "MySQL 8" |

## Out of scope

The 8.4 -> 9.7 LTS hop. Driver version bumps (`aiomysql`, `PyMySQL`). Adding a replica — though the absence of one is what makes this a one-way door, and it is worth its own ticket.
