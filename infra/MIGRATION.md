# Migration runbook: managed MySQL + Redis -> self-hosted droplet

Source of truth for moving prod data from DO Managed MySQL + Managed Redis to
the new `pfv-data-01` droplet provisioned by `infra/terraform`.

Plan window: pick a quiet hour. Total downtime: ~10–20 min for the size of the
PFV dataset today. Mostly waiting on dump + import.

## Pre-flight checklist

- [ ] TFC apply on `FlamaCorp/pfv` succeeded; droplet reachable via
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
> 10.42.x.x address until Step 0 below attaches the app to the VPC. Don't try
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

CLI alternative (if you prefer to script it):

```bash
# Edit a copy of .do/app.yaml, set services.backend.instance_count: 0,
# then push that copy. Restore the original count in step 7.
doctl apps update <app-id> --spec /tmp/app.yaml.zero
```

Confirm `/health` returns "service unavailable" (or the route 502s) before
moving on.

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

Wait for `/api/v1/health` and `/api/v1/ready` to go green.

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

## Posture notes

- MySQL listens on `0.0.0.0`. MySQL 8 only accepts a single `bind-address`,
  so we rely on the DO Cloud Firewall + host ufw to restrict to VPC. Both
  layers must allow 3306 from the VPC CIDR for App Platform to connect.
- Redis is bound to `127.0.0.1 <private_ipv4>` and `requirepass` is set.
  Auth + VPC + protected-mode all required.
- No TLS on either service. Traffic stays inside DO's VPC; revisit if we
  add a second region or move to a multi-tenant network.
- Backups: nightly logical dump in `/var/backups/mysql/` (7-day retention)
  plus DO weekly droplet snapshots. To restore: copy a `.sql.gz` off the
  droplet, `gunzip -c <file> | mysql pfv2`. Or restore the snapshot from the
  DO console and re-point DNS / spec at the new droplet.
