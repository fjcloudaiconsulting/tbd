# Migration runbook: managed MySQL + Redis -> self-hosted droplet

Source of truth for moving prod data from DO Managed MySQL + Managed Redis to
the new `<data-droplet>` droplet provisioned by `infra/terraform`.

> **Status (2026-05-07).** The cutover described here has already been
> executed; production runs against `<data-droplet>` and the managed services
> have been decommissioned. The runbook is kept as a reference for any
> future managed-to-droplet move (or as the canonical writeup of the path
> we took). Variables like `<managed-host>`, `<APP_ID>`, and the secret
> blobs are illustrative for any next time; do not re-run the steps as-is.

Plan window: pick a quiet hour. Total downtime: ~10–20 min for the size of the
PFV dataset today. Mostly waiting on dump + import.

> **MySQL 8.0 -> 8.4 upgrade (TBD-360) — DONE 2026-08-19. Production runs
> 8.4.11.** The ordered checklist is in
> [`MYSQL-84-CUTOVER.md`](MYSQL-84-CUTOVER.md) and the executed record, with the
> deviations, is in `specs/2026-08-18-mysql-84-cutover-record.md`; the full
> analysis is in `specs/2026-08-09-mysql-84-lts-upgrade.md`.
>
> ⚠⚠ **The scale-to-0 procedure described further down this file does not
> work, and its CLI form fails SILENTLY.** `doctl apps update --spec` with
> `instance_count: 0` exits 0 and prints a plausible spec while changing
> nothing — `0` is Go's zero value and is dropped by `omitempty` before the
> request is sent. That is precisely how an impossible step survived here as a
> "tested procedure". Step 2 has the full account; TBD-416 replaced the quiesce
> with a bounded metadata-lock wait, recorded under "Quiescing without scaling
> to zero" below.

## Pre-flight checklist

- [ ] TFC apply on `<tfc-org>/<data-workspace>` succeeded; droplet reachable via
      `ssh root@<public_ipv4>` (fetch the IP from TFC outputs or
      `terraform -chdir=infra/terraform output -raw droplet_public_ipv4`).
- [ ] Ansible playbook applied; `mysql --version` and `redis-cli ping` work
      on the droplet.
- [ ] Latest weekly DO backup of the managed DB exists. As an extra belt:
      take a fresh mysqldump from the managed DB endpoint (see step 4) before
      the cutover starts.
- [ ] `doctl` configured and authenticated locally.
- [ ] App Platform spec file checked out and ready to edit (per
      `reference_do_spec_sync.md`: deploy via direct `doctl apps update`,
      not the GitHub deploy action).

> **Note on private-IP reachability.** App Platform cannot reach the droplet's
> <vpc-cidr> address until Step 0 below attaches the app to the VPC. Don't try
> to verify that before Step 0. To verify the *droplet side* end-to-end
> earlier, spin up a one-shot droplet inside the VPC and run
> `mysql -h <droplet_private_ipv4> -u pfv_app -p -e 'SELECT 1'` from there.

## Cutover

### 0. Attach App Platform to the new VPC

App Platform components live in their own DO-managed VPC by default and can't
reach the droplet's private IP until the app is explicitly attached to the
VPC the droplet sits in. Per
[DO's enable-VPC docs](https://docs.digitalocean.com/products/app-platform/how-to/enable-vpc/),
this is a top-level `vpc:` block on the app spec.

1. Get the VPC UUID:

   ```bash
   terraform -chdir=infra/terraform output -raw vpc_id
   ```

2. Edit `.do/app.yaml`, uncomment the top-level `vpc:` block, paste the UUID:

   ```yaml
   vpc:
     id: <vpc-uuid-from-step-1>
   ```

3. Push the spec via `doctl` (NOT the GitHub deploy action — per
   `reference_do_spec_sync.md`, `digitalocean/app_action/deploy@v2` silently
   prefers `app_name` over `app_spec_location`, so the spec file never reaches
   prod via that path):

   ```bash
   doctl apps update <APP_ID> --spec .do/app.yaml
   ```

4. Wait for the deploy to finish, then verify the VPC is attached:

   ```bash
   doctl apps get <APP_ID>
   ```

   The output should include `vpc.id = <vpc-uuid>`. If the field is empty,
   the spec didn't take — re-run the `update` and watch the response, do not
   proceed to "Quiesce the app" until VPC attachment is confirmed.

### 1. Snapshot the managed DB (belt-and-suspenders)

DO control panel -> Databases -> pfv mysql cluster -> Backups -> "Create
backup". Wait until it shows up green. Or via API:

```bash
doctl databases backups list <db-cluster-id>
```

Skip this if the most recent automated backup is fresh enough for your
comfort.

### 2. Quiesce the app

Take the App Platform service offline so no writes happen during the dump.
Easiest path is the DO web console, which avoids hand-crafting a separate
zero-instance spec file:

> **DO console** -> App -> Components -> `backend` -> Settings ->
> Resize -> set instance count to `0` -> Save. App Platform redeploys
> with no backend replica.

⚠⚠ **THIS DOES NOT WORK, AND THE CLI FORM FAILS SILENTLY.** Attempted on
2026-08-19 during the TBD-360 window. There are two distinct failures here, and
the second is the dangerous one:

- **Console:** refused on the legacy `basic-xxs` plan, which pins the component
  to exactly one container ("This plan is limited to 1 container. Plans
  starting at $12.00/mo can manually scale or autoscale"). A loud, visible
  refusal — you cannot mistake it for success.
- **CLI (`doctl apps update --spec`):** `instance_count: 0` never reaches the
  API at all. `0` is Go's zero value and is discarded by `omitempty` during
  serialisation. Measured by local round-trip through `doctl apps spec validate
  --schema-only`: `0` comes back **absent** from the spec, while `1` and `2` are
  preserved. **The command exits 0 and prints a plausible spec while the app
  keeps whatever instance count it already had.**

⚠ The CLI failure is **plan-independent**. No plan bump fixes it, because the
value is dropped client-side before any pricing rule is ever consulted — so the
$12/mo tier cannot buy this mechanism at any price. It is also how the step
survived in a runbook as "tested": running it looks exactly like success. If you
ever run it, **read `instance_count` back off the live spec**; never trust the
exit code.

⚠ Residual, stated honestly: the round-trip proves the *client* cannot transmit
zero, not that the API would reject a zero arriving by some other route.
`doctl` and the GitHub deploy action are the only routes in use, and both
serialise through the same structs.

**There is therefore no verified way to scale this app to zero today.** The
console and CLI steps here are retained as a record of the 2026-05 procedure,
**not** as instructions you can follow. When you need writes bounded rather than
stopped, use "Quiescing without scaling to zero" below, which is what TBD-416
shipped in place of this step.

### 2b. Quiescing without scaling to zero

Since the app cannot be taken offline (step 2), an operation that needs the
database to hold still has to **bound its own waiting** rather than stop the
writers. This is the mechanism TBD-416 settled on, and it is what replaced
"scale backend to 0" everywhere that phrase used to appear.

⚠ **First, separate the two concerns that the old instruction conflated.**

* **Durability** — "we must not lose writes taken during the window". Real when
  rollback means *restore a snapshot*, because the snapshot predates them.
* **Liveness** — "the statement must not hang, and must not wedge the app
  behind it". This is a metadata-lock (MDL) problem, and it has a one-variable
  fix.

They call for different things, and most operations only have the second.
A schema rename, for instance, has **no** durability concern at all: rolling it
back is another `RENAME TABLE`, which carries every intervening write with it.

**1. The statement bounds its own MDL wait.**

⚠⚠ **NOT YET TRUE OF PRODUCTION.** `lock_wait_timeout = 30` is pinned in
`roles/mysql/templates/my.cnf.j2`, but that is a repo change. Until the play is
converged against `<data-droplet>` the live server is still on **31536000**, and
the nightly dump still streams to its final name. Confirm before relying on
either:

```bash
mysql --no-defaults -N -B -e "SELECT @@lock_wait_timeout"   # 30 once converged
```

⚠ **Converging it RESTARTS MySQL.** `roles/mysql/tasks/main.yml` carries
`notify: Restart mysql` on this template, and the play flushes handlers, so the
first converge after this change restarts the single node holding all user data
— with the backend still serving. Schedule it; do not let it ride along on a
run whose purpose is some unrelated knob. (`lock_wait_timeout` is dynamic, so
`SET GLOBAL` can bridge the gap, but it does not survive a restart and the play
would not notice the drift — the file is still the source of truth.)

Once converged, the generated rename artifact additionally carries its own
stricter `SET SESSION lock_wait_timeout = 10;` as its first **executable** line
(two `--` comment lines precede it). Measured on MySQL 8.4.11, production's
exact version, 2026-08-27:

```
holder: START TRANSACTION; SELECT COUNT(*) FROM src.child;   -- holds SHARED_READ MDL

  RENAME with SET SESSION lock_wait_timeout = 3
    -> ERROR 1205 (HY000): Lock wait timeout exceeded, in 3s

  RENAME with no bound (the 31536000s default)
    -> still blocked at 13s with NO error returned; had to be killed
```

⚠ The bound must be **inside the same session** as the statement it protects.
The runbook pipes the whole artifact into one client (`mysql --no-defaults <
rename.sql`), so a `SET` issued as a separate `mysql -e` is a different session,
evaporates before the statement runs, and reads in the runbook as though it did
something. `bin/gen-rename-sql.sh` emits the line and **refuses to produce an
artifact without it**, ahead of the `RENAME`.

⚠ The session value is deliberately **below** the server value. The server pin
is also the *victim's* timeout — what an ordinary app query waits while queued
behind a pending exclusive MDL. Session below global means the DDL yields first
and the queue drains with no user-visible errors. Inverted, real users get 1205
while the statement keeps waiting.

**2. Silence the largest MDL holder first**, by pre-taking the scheduler's tick
lock:

⚠⚠ **Do NOT use a bare `redis-cli` here.** Production Redis sets `requirepass`
(`roles/redis/templates/00-static.conf.j2`), and **`redis-cli` EXITS 0 ON AUTH
FAILURE** — it prints `NOAUTH Authentication required.` on *stdout* and returns
0, so an unauthenticated `SET` looks exactly like a successful one. That is
measured and already written up at `roles/redis/handlers/main.yml:33-39`. Use
the authenticated form and **check the reply**:

```bash
REDISCLI_AUTH='<redis_password>' \
  redis-cli --no-auth-warning SET scheduler:tick:lock 1 EX 1800
# MUST print exactly: OK
# Anything else -- NOAUTH, an error, empty -- means NOTHING WAS SET.
```

`acquire_tick_lock` uses `SET ... nx=True`
(`backend/app/services/scheduler/loop.py:22`), so a key that already exists makes
the tick log `scheduler.tick.skip_locked` and return (`loop.py:27-29`). One
command, no deploy, and the TTL is a built-in dead man's switch — it undoes
itself even if you are interrupted.

⚠ This blocks the NEXT tick; it does not stop one already running. The operator
`SET` deliberately omits `NX` so it takes effect regardless of current state,
which means it can also overwrite a live tick's lock — and `run_one_tick` never
deletes the key. So confirm no tick is in flight before you rely on this:

```bash
REDISCLI_AUTH='<redis_password>' redis-cli --no-auth-warning TTL scheduler:tick:lock
```

A TTL close to the tick lock's own (not the 1800 you just set) means a tick is
running; wait it out rather than proceeding.

**3. Look before you leap — at the right table.**

```sql
SELECT m.OBJECT_SCHEMA, m.OBJECT_NAME, m.LOCK_TYPE, m.LOCK_STATUS,
       t.PROCESSLIST_ID, t.PROCESSLIST_USER, t.PROCESSLIST_TIME
  FROM performance_schema.metadata_locks m
  JOIN performance_schema.threads t ON t.THREAD_ID = m.OWNER_THREAD_ID
 WHERE m.OBJECT_TYPE = 'TABLE' AND m.LOCK_STATUS = 'GRANTED';
```

⚠⚠ **Select `PROCESSLIST_ID`, never `OWNER_THREAD_ID`.** They are different
numbers and `KILL` takes the former. Measured on 8.4.11: the same session was
`THREAD_ID = 332` and `PROCESSLIST_ID = 295`. Killing the Performance Schema
thread id mid-incident either errors or **terminates an unrelated
connection** — hence the join above.

⚠⚠ **Do NOT gate on `information_schema.innodb_trx` being empty.** That is not
the metadata-lock table, and the difference is not academic — measured
2026-08-27 on 8.4.11, a session holding a table MDL via `LOCK TABLES` showed
`innodb_trx rows: 0` while `metadata_locks` showed the granted lock, and it
still blocked a `RENAME`. Asserting `innodb_trx` empty and concluding "no MDL
blockers" is a false all-clear from querying the wrong table.

This is a glance, not a gate: the bound in (1) is what makes the operation safe.

**4. `KILL` only as an evidenced escalation**, when the query above names a
specific blocking session — and then `KILL <PROCESSLIST_ID>`, the joined column,
not `OWNER_THREAD_ID`. Never pre-emptively.

⚠ **The same bound applies to the alembic PRE_DEPLOY job, as an *actor*.** It
issues `ALTER TABLE` against this server whenever a revision is pending and sets
no session bound of its own, so it inherits the 30s. That is the trade this pin
exists to make — previously such an `ALTER` could wait up to 365 days while its
pending exclusive MDL queued the entire application behind it. The residual cost
is real and worth knowing: MySQL DDL auto-commits per statement, and several
revisions carry many DDL operations (`045_reconciliation_state.py` has 13), so a
1205 partway leaves that revision half-applied with `alembic_version` unstamped,
and the retry dies on `Duplicate column name` until someone clears it by hand.
`backend/scripts/migrate.py` already bounds the blast radius to one revision by
driving alembic per revision. **A deploy landing inside the backup window below
is the likeliest way to meet this**, and unlike an operator the release pipeline
has no instruction to stay out of it.

**5. Stay off 02:00–02:30 UTC.** `mysql_backup_cron_hour: "2"`
(`roles/backups/defaults/main.yml:4-5`) and `mysqldump --single-transaction`
takes a shared MDL on each table as it reads it and holds it to end of
transaction. It is a guaranteed collision, and it was absent from every version
of this runbook.

⚠ Once the pin is converged, the collision costs a **failed** nightly backup
rather than a hung one. That is the better failure only because the dump now
writes to a temporary name and is renamed into place on success — without that,
a 1205 mid-dump leaves a *structurally valid* gzip of a truncated dump at the
final name, which `gzip -t` happily passes. See the comment block in
`roles/backups/templates/mysql-backup.sh.j2`.

⚠ **Since TBD-400, something does observe it** — but from off the box, not on
it. `.github/workflows/backup-freshness-probe.yml` runs at 04:17 UTC, lists the
bucket's object metadata through a read-only role, and opens a deduped
`[backup-stale]` GitHub issue when the newest manifest is older than 25 hours,
missing, or sits beside an implausibly small dump.

⚠⚠ It runs in CI **because an alert emitted by the droplet cannot fire when the
droplet is gone, or when its cron never ran** — which is exactly the disaster the
backup exists for. The failure mode is silence, and only an external observer can
read silence. The cron's `>> /var/log/mysql-backup.log 2>&1` is still not an
alarm, and there is still no MTA anywhere in `infra/`; that is fine, because
nothing now depends on anyone reading that log.

### 2c. Restoring from the off-host copy (TBD-400)

The nightly dump is copied to S3 in the company AWS account
(`884686184019`, `eu-central-1`). ⚠ **The droplet cannot read it back.** Its
credential is put-only and is explicitly denied `kms:Decrypt` in the key policy,
so a restore is done from a workstation with a different principal.

Each night writes three objects under `pfv-data-01/YYYY/MM/DD/`:

| Object | What it is |
|---|---|
| `pfv2_<ts>.sql.gz` | the schema + data dump |
| `grants_<ts>.sql.gz` | `CREATE USER` / `GRANT` for every account |
| `manifest_<ts>.json` | keys, byte sizes, SHA-256s, table count, MySQL version |

⚠ **The manifest is written LAST and is the completion marker.** S3 has no
rename, so the `.part` trick does not lift. A day prefix without a manifest is a
night that did not finish, however plausible the other objects look.

```bash
# 1. Pick the night and confirm it completed.
aws s3 ls s3://tbd-mysql-backups-884686184019/pfv-data-01/2026/08/28/
aws s3 cp  s3://tbd-mysql-backups-884686184019/pfv-data-01/2026/08/28/manifest_<ts>.json - | jq .

# 2. Pull both artifacts and check them against the manifest, not against hope.
aws s3 cp s3://.../pfv2_<ts>.sql.gz .
aws s3 cp s3://.../grants_<ts>.sql.gz .
sha256sum pfv2_<ts>.sql.gz grants_<ts>.sql.gz     # must match the manifest

# 3. Restore GRANTS FIRST, then data.
zcat grants_<ts>.sql.gz | mysql --force     # --force: skips reserved mysql.* accounts
zcat pfv2_<ts>.sql.gz   | mysql pfv2
```

⚠ `--force` on the grants file is deliberate, but NOT for the reason you might
assume: the generator already excludes `mysql.sys` / `mysql.session` /
`mysql.infoschema`. It is needed because accounts that a fresh server already
has -- `root@localhost` above all -- will collide, and a collision must not
abort the rest of the file. Do not drop the flag after checking for the reserved
accounts and finding them absent.

⚠ **Grants are a TBD-360 rollback dependency, not a nicety.** A `pfv2`-only dump
restores tables and zero logins, so it cannot recreate `pfv_app`.

#### The quarterly restore drill

⚠⚠ **Nothing automated proves a stored byte is readable.** The nightly checks
cover content (before upload), transport (S3's server-side SHA-256 on the PUT)
and presence (the freshness probe's metadata listing). A misconfigured KMS grant
would pass all three every night for months and only surface at a restore. That
gap is closed by doing a restore on purpose, on a schedule:

- [ ] 2026-Q4 restore drill: pull the latest night, restore into a scratch
      container, confirm the table count matches the manifest and that `pfv_app`
      can authenticate. Record the date here.

If quarterly feels too slow, shorten the interval. Do **not** close the gap by
granting the droplet read access; that would undo the property that makes a
stolen droplet key a nuisance rather than a breach.

### 3. Dump from managed MySQL

From a workstation or one-shot droplet that has network access to the managed
DB:

```bash
mysqldump \
  --single-transaction \
  --routines \
  --triggers \
  --quick \
  --hex-blob \
  --set-gtid-purged=OFF \
  -h <managed-host> \
  -P <managed-port> \
  -u doadmin -p \
  --ssl-mode=REQUIRED \
  pfv2 | gzip > pfv2_$(date +%Y%m%d-%H%M%S).sql.gz
```

(`--set-gtid-purged=OFF` keeps the dump portable; the new droplet isn't a
GTID replica.)

Verify the gzip is intact before moving on — a partial dump can silently
import most of the data and you only notice rows are missing during smoke
tests:

```bash
gunzip -t pfv2_*.sql.gz && echo "dump intact"
```

### 4. Import into the droplet

Copy the dump up:

```bash
scp pfv2_*.sql.gz root@<droplet_public_ipv4>:/var/backups/mysql/migration/
```

On the droplet (run as root via `sudo` — root@localhost uses Ubuntu's
default socket-auth plugin, so no password is needed):

```bash
sudo bash -c 'gunzip -c /var/backups/mysql/migration/pfv2_*.sql.gz | mysql pfv2'
```

### 5. Verify

Row counts on the droplet:

```bash
mysql pfv2 -e 'SHOW TABLES'
mysql pfv2 -e 'SELECT COUNT(*) FROM users'
mysql pfv2 -e 'SELECT COUNT(*) FROM transactions'
mysql pfv2 -e 'SELECT COUNT(*) FROM accounts'
```

Counts should match the managed DB. If you can pre-record managed-side
counts in step 3, do so and diff here.

Schema sanity (catches a silent migration drift between source and target):

```bash
# On a workstation with access to both endpoints — managed side:
mysqldump --no-data --skip-comments \
  -h <managed-host> -P <managed-port> -u doadmin -p \
  --ssl-mode=REQUIRED pfv2 | sha256sum

# Droplet side, run as root:
sudo mysqldump --no-data --skip-comments pfv2 | sha256sum
```

The two checksums should match. A diff here means CREATE TABLE / INDEX
DDL drifted somewhere — investigate before proceeding.

### 6. Update App Platform secrets

App Platform stores secrets per-component and does NOT auto-inherit them
across components. The `pfv` spec has THREE secret values that must all be
updated atomically to point at the droplet:

| Component | Secret | New value |
|---|---|---|
| `services.backend.envs[DATABASE_URL]` | `DATABASE_URL` | `mysql+aiomysql://pfv_app:<PASSWORD>@<DROPLET_PRIVATE_IPV4>:3306/pfv2` |
| `services.backend.envs[REDIS_URL]` | `REDIS_URL` | `redis://:<REDIS_PASSWORD>@<DROPLET_PRIVATE_IPV4>:6379/0` |
| `jobs.migrate.envs[DATABASE_URL]` | `DATABASE_URL` | (same as backend's `DATABASE_URL` above) |

**WARNING:** if you only update the backend service's `DATABASE_URL` but
leave the migrate pre-deploy job pointing at the old managed cluster,
future deploys will run Alembic against the OLD database while the app
serves from the NEW one. State diverges silently and you will not notice
until something breaks.

Two ways to apply, in order of preference:

- **DO web console** (RECOMMENDED): App -> Settings -> per-component
  "Environment Variables" -> edit each of the three secret values listed
  above. Saving triggers a redeploy that re-encrypts the new plaintext.
  The plaintext only ever sits in the browser form field; nothing hits
  the local filesystem.
- **Spec file + doctl** (only if you must script it): edit `.do/app.yaml`,
  replace the three encrypted `EV[...]` values with the new plaintext
  (App Platform re-encrypts on save), push, then **immediately** overwrite
  the file with the re-encrypted spec from step 9 below.

  ```bash
  doctl apps update <APP_ID> --spec .do/app.yaml
  ```

  > **Footgun.** The committed `.do/app.yaml` briefly contains plaintext
  > `DATABASE_URL` / `REDIS_URL` on disk between edit and the next git
  > checkout. Do not commit / push the file in that state, do not
  > screenshot it, and proceed to step 9 to fetch the re-encrypted spec
  > before doing anything else.

  Per `reference_do_spec_sync.md`, this MUST be a direct `doctl` push;
  the `digitalocean/app_action/deploy@v2` GH Action silently prefers
  `app_name` over `app_spec_location` and will not push the file.

The `EV[...]` form for each secret can be retrieved from the live spec
afterwards:

```bash
doctl apps spec get <APP_ID> --format yaml
```

Copy the freshly-encrypted blocks back into `.do/app.yaml` so the
committed spec stays authoritative for future deploys.

### 7. Bring the app up

> **CRITICAL.** This step MUST mirror the path you chose in step 6. If
> you took the console path in step 6, the local `.do/app.yaml` still
> contains the OLD `EV[...]` blobs (encrypted to the managed-DB / managed-
> Redis URLs). Pushing that file here via `doctl apps update --spec`
> reverts the live secrets and silently re-points the app at the old
> managed services.

⚠ Both paths below assume step 2 actually scaled the app down. It cannot
today (see step 2), so on a present-day run there is nothing to scale back up
and the only live concern in this step is which copy of the secrets wins.

Pick the matching path:

- **Console path (matches step 6 console option, RECOMMENDED):** in the DO
  web console, App -> Components -> `backend` -> Settings -> Resize -> set
  instance count back to its prior value (1+). Save. App Platform
  redeploys with the new (droplet-pointing) secrets already in place.
  Do NOT push local `.do/app.yaml` from this machine.

- **CLI path (only if step 6 used `doctl apps update --spec`):** push local
  `.do/app.yaml` ONLY after step 9 below has already replaced the three
  `EV[...]` blobs locally with the freshly-encrypted forms. If step 9
  hasn't happened yet, scale back up by editing the live spec instead:

  ```bash
  doctl apps spec get <APP_ID> --format yaml > /tmp/live-app.yaml
  # In /tmp/live-app.yaml: bump services.backend.instance_count back to 1+.
  doctl apps update <APP_ID> --spec /tmp/live-app.yaml
  ```

Watch the deploy:

```bash
doctl apps logs <app-id> --type RUN --follow
```

Wait for `/health` and `/ready` to go green, then check `/health/dependencies`.
(These are served at the app root, not under `/api/v1/` — the `/api/v1/health` and
`/api/v1/ready` paths this line used to name have never existed.) `/ready` covers the
database only; `/health/dependencies` is the one that also covers Redis.

### 8. Smoke test (manual)

- [ ] Log in.
- [ ] Open the dashboard. Charts render.
- [ ] Create a transaction.
- [ ] Run a CSV import.
- [ ] Hit the rate-limited endpoint a few times rapidly. Expect 429 after
      threshold (confirms Redis rate-limit storage works).
- [ ] Check `/var/log/mysql/slow.log` on the droplet for any unexpected
      slow queries during the smoke test.

### 9. Persist the live spec to `.do/app.yaml` (REQUIRED before next deploy)

> ✅ **This step is now ENFORCED, not merely documented (TBD-425).**
> `scripts/ci/assert-app-spec-secrets-synced.sh` runs in both deploy paths
> before the spec is pushed, and REFUSES the deploy when a committed secret
> differs from the live app, naming every one that would be overwritten.
>
> ⚠ It exists because this step was skipped and the warning below was not
> enough. On 2026-08-20 a deploy pushed stale `DATABASE_URL` and `REDIS_URL`
> blobs over the working ones and took production's database and redis
> credentials down:
> `(1045, "Access denied for user 'pfv_app'@'10.42.0.3'")`. The app auto-rolled
> back, but `main` was undeployable until the values were corrected by hand.
>
> The break-glass path (`deploy.yml`) can still override, deliberately, via its
> `allow_secret_drift` input. `release.yml` cannot — the automatic path must
> never be able to overwrite production's secrets silently.

> **Why this step exists.** The GitHub Actions deploy workflow at
> `.github/workflows/deploy.yml` pushes the committed `.do/app.yaml` as
> the authoritative spec on every merge to `main`. After the cutover
> above, the **live** spec on App Platform has the correct `vpc.id` and
> the new encrypted `EV[...]` secret blobs for `DATABASE_URL` (backend
> and migrate job) and `REDIS_URL`. The **committed** file does not.
> If you skip this step, the next normal deploy reverts the live spec
> back to the committed file, dropping VPC attachment and / or pointing
> secrets at whatever was there before.

This gate runs after smoke tests pass and BEFORE you decommission the
managed databases (so you can roll back if you find a problem in the
diff).

#### 9a. Fetch the live spec

```bash
doctl apps spec get <APP_ID> --format yaml > /tmp/live-app.yaml
```

#### 9b. Diff against the committed file

```bash
diff -u .do/app.yaml /tmp/live-app.yaml | less
```

The diff should show exactly:

- `vpc:` block at the top level with the real VPC UUID (uncommented).
- `services.backend.envs[DATABASE_URL]` — new `EV[...]` blob.
- `services.backend.envs[REDIS_URL]` — new `EV[...]` blob.
- `jobs.migrate.envs[DATABASE_URL]` — new `EV[...]` blob.

Anything else (instance counts, regions, env-var values you didn't
touch) MUST match. If something else differs, investigate before
proceeding — the live spec may have drifted from its source-of-truth.

#### 9c. Update the committed file

Copy the verified differences from `/tmp/live-app.yaml` into
`.do/app.yaml`. Keep the existing comments / structure intact; only
swap the four target sections (vpc + three EV blobs).

#### 9d. Commit and push

```bash
git checkout -b chore/post-cutover-spec-persist
git add .do/app.yaml
git diff --staged                       # final read-through
git commit -m "chore(infra): persist post-cutover app spec (vpc + 3 EV secrets)"
git push -u origin chore/post-cutover-spec-persist
gh pr create --title "chore(infra): persist post-cutover app spec" --body "Reflects the live App Platform state after the managed-to-droplet cutover. Required before any subsequent main deploy or the GH Actions workflow will revert vpc.id and rotate secrets back to pre-cutover values."
```

Merge that PR before any other change lands on `main`. Until it's
merged, **do NOT** trigger a deploy via merge-to-main: it will overwrite
the live spec.

### 10. Decommission grace period

Keep the managed DB and Redis running for 24h with no writes. If anything
goes wrong you can flip `DATABASE_URL`/`REDIS_URL` back and redeploy
(rollback section below).

After 24h of clean operation AND step 9 has merged:

```bash
doctl databases delete <mysql-cluster-id>
doctl databases delete <redis-cluster-id>
```

## Rollback

If smoke tests fail or production behaves badly:

1. Revert the App Platform spec change (replace `DATABASE_URL` and
   `REDIS_URL` with the managed endpoints).
2. `doctl apps update <app-id> --spec .do/app.yaml` to redeploy with the
   prior config.
3. Investigate the droplet path before retrying. Common causes:
   - VPC peering not actually attached (check the droplet is in the VPC
     used by App Platform).
   - MySQL `bind-address` left at `127.0.0.1` (check
     `/etc/mysql/mysql.conf.d/pfv.cnf`).
   - ufw blocking the connection (check `ufw status verbose`).
   - Firewall rule missing (`doctl compute firewall list`).
   - Wrong password on `pfv_app` (compare the App Platform secret value
     with what was set in `infra/ansible/inventory.yml` /
     `mysql_app_password`; root@localhost is socket-auth, no password to
     check there).

## Data-plane package pins (TBD-419)

The MySQL packages on `<data-droplet>` are held in the dpkg database, and the
ansible play no longer upgrades packages on a routine converge. Both halves are
declared in `infra/ansible/roles/common/`; neither is hand-applied box state any
more.

### What is pinned, and what is not

| | State | Why |
|---|---|---|
| `mysql-apt-config` | **held** | Its postinst rewrites `/etc/apt/sources.list.d/mysql.list` from debconf, i.e. it decides which MySQL major track is enabled. This is the package the 2026-08-19 near miss was about. |
| `mysql-community-*`, `mysql-server*`, `mysql-client*`, `mysql-common` | **held** | A MySQL major jump is not reversible in place, and 9.x is an Innovation release (quarterly EOL, not LTS). |
| `redis-server` | **not held, deliberately** | Ubuntu noble ships one Redis major and its security pocket only ever ships 7.0.x, so there is no track to jump. Holding it would permanently block security patching on a VPC-facing service to prevent something that cannot happen. |
| Everything else | **not held** | `unattended-upgrades` applies `noble-security` daily, unchanged. |

The hold set is **derived, not literal**: it is the declared candidate list
intersected with what `package_facts` reports as installed. `dpkg_selections`
hard-fails on a package the host does not have, and production (Oracle
`mysql-community-server`) and a scratch droplet (Ubuntu `mysql-server`) run
different package families, so a static list breaks one of them.

⚠ **What the pins cost.** MySQL patch releases (8.4.11 to 8.4.12) no longer
arrive on their own. That is the intended trade: they are a database restart,
and this node has no HA. They are applied by the procedure below.

### The routine converge does not touch packages

`ansible.builtin.apt: upgrade: safe` still exists but carries `tags: [patch,
never]`, so it runs only when the tag is typed on the command line. It is not
behind a variable on purpose: a variable can be set from role defaults,
`group_vars`, `inventory.yml` or the extra-vars file `run-playbook.sh` builds,
none of which the operator sees at the moment they hit return.

### Deliberately moving MySQL or Redis forward

This is a **windowed operation with a snapshot**. It does not ride along with a
config change.

1. **Snapshot first.** DO droplet backups are off at the IaC level
   (`enable_backups = false`), so take an explicit one and wait for it to
   complete:
   ```bash
   doctl compute droplet-action snapshot <droplet-id> --snapshot-name pre-patch-$(date +%F) --wait
   ```
   Also take a logical dump: `ls -lh /var/backups/mysql/` and confirm the
   nightly file is from today, or run the backup script by hand.
2. **Rehearse on a throwaway droplet, not on production:**
   ```bash
   infra/ansible/bin/run-playbook.sh --scratch-host <ip> --scratch-private-ip <ip> -- --tags patch
   ```
3. **Pre-flight production read-only.** A clean dry run here is meaningful for
   the repo-track fence specifically, because that fence runs under `--check`:
   ```bash
   infra/ansible/bin/run-playbook.sh --production --check --diff
   ```
   ⚠ Do not tee that to a world-readable file; the template diffs contain the
   MySQL and Redis passwords in cleartext. ⚠ No longer true since TBD-414 —
   both tasks carry `no_log: true` and their diffs are censored.
4. **Pick a quiet hour** and announce the window. A MySQL package upgrade
   restarts the database; a Redis one drops every client connection.
5. **Run the patch path.** The holds and the repo-track fence carry
   `tags: [always]`, so they still execute on this invocation, and the fence
   re-runs *after* the upgrade — the upgrade can itself move
   `mysql-apt-config`, whose postinst re-points the repo:
   ```bash
   infra/ansible/bin/run-playbook.sh --production -- --tags patch
   ```
   Held packages will be reported as `kept back`. That is correct: the patch
   task does not move MySQL.
6. **To move MySQL forward on purpose**, unhold the **whole resolved set**, move
   it, and let the next converge re-apply the holds.

   ⚠⚠ **Unholding one package does not work, and fails in the middle of the
   window.** Oracle's `mysql-community-server` depends on `mysql-common`,
   `mysql-client`, `mysql-community-client`, `mysql-community-client-core` and
   `mysql-community-client-plugins`, and every one of those `Depends:` is a
   `= <version>` equality. So with only the server unheld, `apt-get install
   --only-upgrade mysql-community-server` cannot pull the client packages it now
   requires, and apt exits 100 with `E: Unable to correct problems, you have
   held broken packages` — the exact failure measurement M6 in
   `specs/2026-08-22-dataplane-package-pins.md` records, at the worst possible
   moment. `--only-upgrade` cannot rescue it either: the blocker is the held
   dependencies, not the addition of new ones.

   Unhold everything the play holds, which is what `apt-mark showhold` prints:

   ```bash
   # (a) See exactly what is held, and keep the list -- it is the set to restore.
   apt-mark showhold | tee /tmp/tbd-holds.txt

   # (b) READ THE LIST FIRST. `showhold` reports every hold on the box, not
   #     only the ones this play declares; anything in it that is not in
   #     roles/common/defaults/main.yml's mysql_hold_candidates was put there
   #     by hand, for a reason nothing in this repo records, and the play will
   #     NOT restore it in step 7. Then unhold, without hand-picking within the
   #     MySQL set:
   xargs -r apt-mark unhold < /tmp/tbd-holds.txt

   # (c) Move MySQL. `install` (not `--only-upgrade`) so a renamed or newly
   #     required dependency can come in; step 3's repo-track pre-flight is what
   #     proved this is a patch and not a major jump.
   apt-get update
   apt-get install -y mysql-community-server

   # (d) Prove it came back.
   systemctl status mysql
   mysql --no-defaults -N -B -e "SELECT VERSION()"
   ```

   ⚠ `apt-get install` here is deliberate, and it is why steps 1 and 3 of this
   procedure are not optional. Unpinned, apt installs whatever the repo offers:
   the snapshot (step 1) and the repo-track pre-flight (step 3) are the only
   things standing between this command and a major jump.

7. **Re-converge to restore the pins.** Run the play with **no tags**. It
   re-holds the packages, re-runs the read-back assert (so "held" becomes
   something the dpkg database confirmed, not something a module claimed), and
   re-asserts the running configuration.

   ⚠ **Do not skip this or leave it for later.** Between step 6 and this run the
   data droplet has NO MySQL pin at all, and `unattended-upgrades` runs daily.
   Diff `apt-mark showhold` against `/tmp/tbd-holds.txt` afterwards and confirm
   the set came back.
8. **Verify the app, not just the box.** Confirm the mysql and redis roles'
   running-config fences passed in step 7, then check `/ready`, a login, and
   `SELECT VERSION()` from the application side.

⚠ **A MySQL MAJOR move is not this procedure.** 8.4 to 9.x removes
`mysql_native_password` entirely, cannot be reversed in place, and the whole
evidence base in this repo — `Migration Checks`, both rehearsal scripts, the
driver verification — is 8.4-only. See `MYSQL-84-CUTOVER.md`, and expect a
rehearsal on a scratch droplet before anything touches production.

⚠ **If the repo-track fence fails, do not clear it by unholding and
upgrading.** It means `apt` is offering a different `major.minor` from the one
this host runs, which is repo drift, not a pending patch. Check
`debconf-show mysql-apt-config` (`select-server` must be `mysql-8.4-lts`) and
`/etc/apt/sources.list.d/mysql.list` against the preseed in
`MYSQL-84-EXECUTE.md`.

## Credential rotation (TBD-414)

Rotating `mysql_app_password`, `mysql_backup_password` or `redis_password`.
All three are Terraform-generated and live in TFC state.

⚠⚠ **The obvious four-step version of this is what caused the 2026-08-20
production outage.** Re-encrypting the values in the DO console and stopping
there leaves the committed `.do/app.yaml` holding the OLD `EV[...]` values, and
`digitalocean/app_action/deploy@v2` pushes that file as authoritative on the
next deploy — silently reverting the credentials you just fixed. Step 5 below
is not optional, and `scripts/ci/assert-app-spec-secrets-synced.sh` will now
block the deploy if you skip it.

### Order

The ordering below is not stylistic. Steps 3 and 4 the other way round fires a
deploy whose `migrate` PRE_DEPLOY job authenticates with the new password
against a server that still holds the old one; it fails at 6/12, App Platform
keeps the previous deployment alive, and a second deploy is needed. That cost
roughly seven minutes of the 24-minute outage on 2026-08-19.

1. **Force new values.** `random_password` keeps its value in state across
   applies; it regenerates only if the resource is tainted or its `keepers`
   change. Either taint the three resources in TFC, or add a `keepers` map to
   them in `infra/terraform/main.tf` and bump it.

   ⚠ Adding `keepers` where there were none is itself a change, so it
   regenerates on the first apply. Do it in the sitting you intend to complete
   the rotation, not ahead of time — between the apply and step 3, TFC state
   and the live box disagree.

2. **Confirm & Apply in TFC.** Terraform is VCS-driven; the apply is manual on
   merge. Nothing on the droplet has changed yet — state now holds new values
   the box has never seen.

3. **Run the play.** `infra/ansible/bin/run-playbook.sh --production`. This
   rotates both MySQL accounts, rewrites the Redis drop-in, and is what
   actually puts the new credentials on the box.

   Since TBD-419 the play no longer performs an unbounded `apt upgrade` as a
   side effect, so this step is safe to run for a credential change alone. The
   MySQL packages are held; `redis-server` deliberately is not.

4. **Re-encrypt FOUR values in the DO console.** Do not take that count on
   trust — the enumeration below has already gone stale once. Read it off the
   spec:

   ```bash
   grep -n 'key: DATABASE_URL\|key: REDIS_URL' .do/app.yaml
   ```

   As of 2026-08-27 that is `services.backend` `DATABASE_URL` and `REDIS_URL`,
   and `jobs.migrate` `DATABASE_URL` and `REDIS_URL`. **The migrate job binds
   its own copy of BOTH.**

   ⚠ Step 6's table above says THREE. It predates 2026-08-20, when the migrate
   job gained its own `REDIS_URL`, and is stale. Missing that fourth binding is
   silent: `assert-app-spec-secrets-synced.sh` compares committed against live,
   so it happily syncs a stale value, and `Settings.redis_url` defaults to `""`
   — so the rotation "succeeds" and the first PRE_DEPLOY job that touches Redis
   fails on a credential the runbook said was rotated.

5. **Sync the re-encrypted spec back into `.do/app.yaml` and commit it**, per
   step 9 above. This is the step that was skipped in 2026-08-20.

6. **Redeploy and verify** `/ready`, `/health/dependencies`, and a real login —
   not just a 200 from the health endpoint.

⚠ **`--check --diff` no longer shows you the Redis config diff.** The task that
installs `00-static.conf` now carries `no_log: true`, so its diff is censored.
`MYSQL-84-EXECUTE.md` used to tell you to confirm that diff touches only the
`requirepass` line, because the same file carries `bind`, and a wrong `bind`
means Redis stops listening on the address App Platform uses. That check is no
longer available from the dry run. Verify `bind` instead by reading the rendered
template source against the inventory before the run, and rely on the role's own
live `bind` fence — which runs after apply, and is what actually catches it.

### Why these are quotable in the first place

The 2026-08-19 exposure happened because `--check --diff` prints a template's
rendered content, and two templates render credentials (`root.my.cnf.j2`,
`00-static.conf.j2`). Those tasks, and the three `mysql_user` tasks that pass a
cleartext `plugin_auth_string`, now carry `no_log: true`, fenced by
`backend/tests/test_ansible_secret_task_no_log.py`.

⚠ One task deliberately omits `no_log` and is allowlisted there: the Redis
read-back passes its credential via `environment:` rather than argv, and
keeping its stderr visible is the only way a Redis auth failure is diagnosable
(that is the signal TBD-412 needed).

## Posture notes

- MySQL listens on `0.0.0.0`. MySQL 8 only accepts a single `bind-address`,
  so we rely on the DO Cloud Firewall + host ufw to restrict to VPC. Both
  layers must allow 3306 from the VPC CIDR for App Platform to connect.
- Redis is bound to `127.0.0.1 <private_ipv4>` and `requirepass` is set.
  Auth + VPC + protected-mode all required.
- No TLS on either service. Traffic stays inside DO's VPC; revisit if we
  add a second region or move to a multi-tenant network.
- Backups: nightly logical dump in `/var/backups/mysql/` (7-day retention)
  is the only durability floor; DO droplet snapshots are off at the IaC
  level (`enable_backups = false` in `infra/terraform/main.tf`) — except
  temporarily during the TBD-360 MySQL 8.4 migration, when TBD-399 turns them
  on with a daily policy and reverts afterwards. To restore:
  copy a `.sql.gz` off the droplet, `gunzip -c <file> | mysql pfv2`. If
  snapshots are ever re-enabled, restoring from the DO console and
  re-pointing the spec at the new droplet is the alternate recovery path.
