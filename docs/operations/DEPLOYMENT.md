# DEPLOYMENT.md

Audience: a contributor who just cloned the repo and wants to understand what happens between `git push` and a live change at `app.thebetterdecision.com` or `thebetterdecision.com`. Also a triage reference for CI/CD failures.

All five pipelines described here are live on `main` today (`test.yml`, `release.yml`, `deploy.yml`, `apex-deploy.yml`, `test-durations.yml`). The apex landing is public at `https://thebetterdecision.com` (and `https://www.thebetterdecision.com`, which 301-redirects to the apex).

For "how do I get my code ready to push", read [`CONTRIBUTING.md`](../../CONTRIBUTING.md). For the env var matrix, read [`ENVIRONMENT.md`](ENVIRONMENT.md). For the managed-to-droplet data move, read [`infra/MIGRATION.md`](../../infra/MIGRATION.md). This file does not duplicate any of them.

## 1. Overview

Four production surfaces. Each has its own pipeline. Some changes fan out across more than one.

| Surface | URL | Hosted by | Updated by |
|---|---|---|---|
| App (FastAPI + Next.js dashboard) | `https://app.thebetterdecision.com` | Single-node k3s cluster, namespace `tbd-prod` (aws-infra) | `release.yml` publishes the `vX.Y.Z` images; merging the Renovate bump PR in aws-infra deploys them |
| Apex landing (marketing, privacy, terms, docs) | `https://thebetterdecision.com` | Cloudflare Worker `tbd-landing` | `apex-deploy.yml` (auto) |
| Data plane (MySQL 8 + Redis) | private VPC IP `<vpc-ip>:3306 / :6379` | Self-hosted DO droplet `<data-droplet>` | TFC workspace `<tfc-org>/<data-workspace>` (manual confirm) |

```mermaid
flowchart LR
  dev[Contributor push to main] --> rel[Release workflow]
  dev --> apex[Apex Deploy workflow]
  dev --> tfc1[TFC data workspace]

  rel -->|release-please release created| do[DO App Platform pfv]
  do --> appurl[app.thebetterdecision.com]

  apex -->|build + wrangler deploy| worker[Cloudflare Worker tbd-landing]
  worker --> apexurl[thebetterdecision.com]

  tfc1 -->|Confirm and Apply| droplet[<data-droplet> droplet]
  droplet --> db[MySQL 8 + Redis]
  do -.->|VPC private IP| db

  classDef pipe fill:#eef,stroke:#446
  class rel,apex,tfc1 pipe
```

The data plane is reached by App Platform over the VPC's private IPv4.

## 2. PR lifecycle (`test.yml`)

Source: `.github/workflows/test.yml`.

`test.yml` is the only CI workflow that runs on PRs. It does **not** deploy anything. Its job is to fail loud before code reaches `main`.

Triggers:
- `pull_request` with path filter on `backend/**`, `frontend/**`, or `.github/workflows/test.yml`
- `workflow_dispatch` (manual)

`concurrency.group = test-${workflow}-${ref}` with `cancel-in-progress: true`. Pushing a new commit to the PR cancels the prior run.

Two jobs run in parallel:

| Job | Steps | Failure means |
|---|---|---|
| **Backend Checks** | Python 3.12, `uv sync --locked` (uv version pinned in `test.yml`), `pytest`, then `python -m compileall backend/app` | Pytest failed, or a syntax error slipped in that pytest didn't reach |
| **Frontend Checks** | Node 22, `pnpm install --frozen-lockfile`, `scripts/check-design-tokens.sh`, `pnpm lint --quiet`, `pnpm test`, `pnpm build` | One of: design-token violation, lint error, test failure, production build failure |

Both must pass for merge (branch protection rule).

```mermaid
flowchart LR
  push[PR push] --> filter{Path in backend/<br/>frontend/<br/>test.yml?}
  filter -->|no| skip[Skip: no checks fire]
  filter -->|yes| parallel
  parallel --> be[Backend Checks]
  parallel --> fe[Frontend Checks]
  be --> bep[pytest]
  be --> bec[compileall app/]
  fe --> fei[pnpm install --frozen-lockfile]
  fe --> fed[check-design-tokens.sh]
  fe --> fel[pnpm lint --quiet]
  fe --> fet[pnpm test]
  fe --> feb[pnpm build]
  bep & bec & fed & fel & fet & feb --> merge[Both jobs green = mergeable]
```

How to read failures:
- **Design tokens**: `frontend/scripts/check-design-tokens.sh` scans for hard-coded colors / spacings that should use brand tokens. Output names the file and line.
- **Lint**: `pnpm lint --quiet` shows only errors (warnings are tolerated; treat warnings shown in logs as informational).
- **Frontend build**: a build failure here means it will also fail in production. Test locally with `docker compose exec frontend pnpm build`.
- **Pytest**: known-flaky `tests/app/transactions-page.test.tsx` does not run here (that's a Jest test). For backend flake see `~/.claude/projects/-Users-flamarion-src-tbd/memory/` references; otherwise the failure is real.

Re-run a single job from the PR's Checks tab.

### 2b. Shard timing maintenance (`test-durations.yml`)

Source: `.github/workflows/test-durations.yml`. Added by TBD-421.

`test-durations.yml` deploys nothing. It regenerates `backend/.test_durations`,
the per-test timing file `pytest-split` uses to balance `test.yml`'s
`Backend Shard` matrix.

- **Triggers:** `workflow_dispatch`, a monthly `schedule`, and `pull_request`
  limited to changes to the workflow file itself (so a PR editing the generator
  proves it still works).
- **Output:** a `test-durations` artifact. A human downloads it and commits it
  through an ordinary PR.
- **Not a required status check**, and it must never become one — it runs the
  whole suite unsharded and takes ~30 minutes.

⚠ **It is deliberately a separate workflow, not a step in `test.yml`.**
`scripts/ci/await-test-run.sh` gates production releases on the **run-level**
conclusion of `test.yml`, so an artifact upload added there would let a
transient upload failure block a deploy for reasons unrelated to the tests.

⚠ **Do not regenerate the file locally.** `/app/.test_durations` is root-owned
while the backend container runs as uid 1001, so `--store-durations` runs the
whole suite and *then* dies at `pytest_sessionfinish`. Local per-test times are
also measurably not a uniform rescaling of runner times.

`backend/tests/test_test_durations_freshness.py` fails the build when the file
drifts too far from the collected suite.

## 3. Release and image promotion (`release.yml`)

Source: `.github/workflows/release.yml`.

`release.yml` is the **single arbiter** of "should we cut a release". It runs on every push to `main` and uses **release-please** (via the Release GitHub App token, environment `release`): an ordinary merge only opens or updates the release PR (`chore(main): release X.Y.Z`), which accumulates every change; a release happens exactly once, when the owner merges that PR. On that merge `release` tags `vX.Y.Z` on the release commit and publishes the GitHub Release (`release_created`). Only then do the gated jobs run: `promote` (shared promote-release workflow retags the `sha-<7>` GHCR images `ghcr.io/fjcloudaiconsulting/tbd/{backend,frontend,migrations}` built by `test.yml` on that commit as `vX.Y.Z`) and `release-smoke` (shared smoke workflow boots those images with `compose.smoke.yaml`, runs the migrations twice, and checks `/health` returns the version and revision). Before `release` runs, `await-tests` waits for the `Test` workflow on this sha, and `release` additionally waits for the `Test` run of the merged release PR's commit when that is not this run's commit.

**Nothing in this repo deploys.** Production is the k3s cluster in `fjcloudaiconsulting/aws-infra`: Renovate opens a PR there bumping the `vX.Y.Z` image tags in `clusters/platform/tbd-prod/`, and merging it is the deploy (Flux applies it). aws-infra's `release-drift-probe` opens an issue when a published release has not reached `clusters/`.

### Trigger

```yaml
on:
  push:
    branches: [main]
```

There is **no `paths:` filter** (TBD-424). Every push must reach release-please, or the release PR goes stale and the merged one is never tagged. The ship/no-ship call is the owner merging the release PR; conventional commit types (`feat:`, `fix:`, etc) only decide the version bump and CHANGELOG section.

Why this design: merging a `feat:`/`fix:` PR no longer releases by itself, so versions ship once per release, when the owner decides, instead of on every merge.

### Job graph

```mermaid
sequenceDiagram
  participant Owner
  participant GH as GitHub Actions
  participant SR as release-please
  participant GHCR as GHCR
  participant RN as Renovate (aws-infra)
  participant K as k3s cluster (Flux)

  Owner->>GH: merge PR to main
  GH->>SR: run release job (after await-tests)
  SR->>SR: analyze conventional commits since last tag
  alt release_created == true (release PR merged)
    SR->>GH: tag vX.Y.Z, GitHub Release
    GH->>GHCR: promote: retag sha-<7> images as vX.Y.Z
    GH->>GH: release-smoke: boot the images (compose.smoke.yaml)
    RN->>RN: open PR bumping tags in clusters/platform/tbd-prod/
    Owner->>RN: merge the bump PR
    RN->>K: Flux applies; migrate init container runs, then backend and frontend roll
  else ordinary merge
    SR-->>GH: open or update release PR, skip promote and release-smoke
  end
```

### The gate

```yaml
promote:
  needs: release
  if: needs.release.outputs.release_created == 'true'
```

This is the load-bearing line (see `.github/workflows/release.yml` for the exact expression). Without `release_created`, `promote` and `release-smoke` do not run and nothing is retagged. The output is set by release-please only when the merge is the release PR. `backend/tests/test_deploy_workflow.py` fences the workflow's shape.

### Production rollout (aws-infra)

The bump PR changes the image tags of the backend, frontend, scheduler and migrations in `clusters/platform/tbd-prod/`. Migrations run as the `migrate` init container of the backend pod (`python /app/scripts/migrate.py`, `migrations` image), so a new version never serves on an old schema; see `https://github.com/fjcloudaiconsulting/aws-infra/blob/main/clusters/platform/tbd-prod/backend.yaml` and Section 8. Following a rollout: [aws-infra runbooks, "Follow Flux and rollouts"](https://github.com/fjcloudaiconsulting/aws-infra/blob/main/docs/runbooks.md).

### Smoke tests

`scripts/smoke-test.sh` runs after every production rollout from aws-infra's `post-deploy-smoke.yml` (INFRA-114), which opens or closes a `[post-deploy-smoke] tbd-prod` issue there; it can also be run by hand. Env: `SMOKE_BASE_URL=https://app.thebetterdecision.com`, plus `SMOKE_USERNAME` / `SMOKE_PASSWORD` for a dedicated smoke user, read from the SOPS Secret `tbd-prod/tbd-smoke`. The smoke user must exist, must be `email_verified`, and must **not** have MFA enabled. The exact command, the credentials' location and rotation are in [aws-infra runbooks, "TBD smoke account"](https://github.com/fjcloudaiconsulting/aws-infra/blob/main/docs/runbooks.md).

#### ⚠ The smoke account cannot have MFA, and that is an accepted risk (TBD-371)

`smoke-test.sh` authenticates with `POST /api/v1/auth/login` and expects a
`TokenResponse`. With MFA enabled that endpoint returns an `MfaChallengeResponse`
(`mfa_required` + `mfa_token`) instead, and the smoke test cannot proceed. Making
it proceed would mean storing the account's **TOTP seed** as a CI secret — a
shared secret that mints valid codes forever, which is strictly worse than no
second factor at all.

So the account stays single-factor. The compensating controls are:

1. **Its username is not published.** It is `secrets.SMOKE_USERNAME` and an App
   Platform SECRET, never a plaintext value in source. Until TBD-371 this was a
   default in `backend/app/config.py` and a plaintext `value:` in
   `.do/app.yaml` — the repository named the weakest authenticated account in
   production and documented that it was weak, in the same breath.
2. **A strong, rotated credential**, `secrets.SMOKE_PASSWORD`.
3. **No PLATFORM rights, and a blast radius of one throwaway org.**

   ⚠ It IS `role: owner` — of its own dedicated organization, and that is not
   avoidable: `register` hardcodes `role=Role.OWNER` and creates a fresh org
   per signup (`routers/auth.py:386-395`), so a standalone account cannot hold
   a lesser role. A lower role would need a second org and an invitation.

   What is actually load-bearing, and what to verify:

   * **`is_superadmin` must be 0.** That is the platform flag, and it is the
     difference between "owner of an empty org" and "owner of the fleet". It is
     written only at construction and has no promote path
     (`is_superadmin=is_first_user_setup`), so only the very first account on
     an install gets it — but verify rather than assume:

     ```bash
     mysql --no-defaults pfv2 -e \
       "SELECT username, is_superadmin, is_founder FROM users WHERE id = 4;"
     ```

   * **Its org holds nothing of value.** The smoke test reads
     `GET /api/v1/categories` and writes nothing, so the org should contain
     only the bootstrap categories. Never point the smoke account at a real
     tenant.

⚠ Usernames are enumerable through `POST /api/v1/auth/check-username` by design,
so a non-published name is not secrecy — it just means an attacker must guess
rather than be handed a confirmed-valid, MFA-less target.

#### Rotating the smoke account

Do this whenever the credential may have been exposed. The account can rename
itself; no database access is required.

**If you do not know the current password** — likely, since it lives only in
`secrets.SMOKE_PASSWORD` and GitHub never shows a secret's value back — recover
it self-service first. This satisfies the rotation on its own, and yields the
login the rename needs:

1. Find the account's email: sign in as a superadmin, `/admin/users`, search the
   username from `secrets.SMOKE_USERNAME`.
2. `POST /api/v1/auth/forgot-password` with `{"email": "<that address>"}`
   (5/minute).
3. Open the link from that mailbox and `POST /api/v1/auth/reset-password` with
   `{"token": "<from the link>", "new_password": "<one you choose>"}`.

⚠ This requires read access to that mailbox. If you do not have it, the admin
email-change endpoint is **not** a way around it: it refuses an already-verified
target with `409 user_already_verified`, deliberately, because repointing a
verified account's address is an account-takeover primitive (TBD-362). The
remaining routes are an out-of-band database write, or standing up a fresh smoke
account and retiring the old one.

⚠ **Renaming is the part that actually remediates the disclosure.** The old
username is in git history permanently, so unpublishing it from HEAD does not
un-know it. Rotating the password alone leaves a known, MFA-less account name
reachable at the public login form.

**If the account's email is fake** — as production's is — the reset above cannot
land and there is no API route in. Registering a replacement does not work
either: `is_founder=True` is hardcoded at registration, so a new account still
needs excluding, and more fundamentally a fake-email account can never verify
while `/login` 403s unverified accounts unconditionally and **no operator
surface can write `email_verified`**. The only route is out-of-band SQL on the
data droplet, which can do the rename and the rotation in one statement without
needing a login at all.

```bash
# 1. Generate the hash with the app's OWN hasher, so the format matches.
#    Read the password from stdin -- never argv, which is world-readable.
printf %s "$NEW_PASS" | docker compose exec -T backend python -c \
  'import sys; from app.security import hash_password; print(hash_password(sys.stdin.read()))'

# 2. On the droplet. ⚠ --no-defaults is REQUIRED: /root/.my.cnf makes a bare
#    `mysql` authenticate as the low-privilege pfv_backup, where reads succeed
#    and only the writes fail -- so a runbook can look like it worked.
mysql --no-defaults -e "SELECT CURRENT_USER()"      # must print root@localhost

# ⚠⚠ QUOTED heredoc ('SQL'), never -e "..." — a bcrypt hash is $2b$12$...,
#    and inside double quotes the shell expands $2, $1 and $12 to EMPTY
#    positional parameters. The UPDATE then succeeds, writing a MANGLED hash,
#    and the account silently cannot log in. Measured 2026-08-31: a hash
#    stored this way begins `b2.OTaD` instead of `$2b$12$`.
mysql --no-defaults pfv2 <<'SQL'
UPDATE users
   SET username       = '<new-name>',
       password_hash  = '<hash from step 1>',
       email          = '<a mailbox you actually control>',
       email_verified = 1,
       password_set   = 1
 WHERE username = '<old-name>';
SQL

# 3. VERIFY. `mysql -e "UPDATE ..."` prints nothing on success AND nothing when
#    zero rows matched, so the write is unevidenced until you look.
mysql --no-defaults pfv2 -e "
  SELECT id, username, email, email_verified, password_set,
         LEFT(password_hash,7) AS hash_prefix
    FROM users WHERE username = '<new-name>';"
#    hash_prefix MUST be \$2b\$12\$. Anything else means the shell ate the
#    dollars and the credential is dead.
```

Then prove the login works **from a host where the password variable exists**,
before a deploy finds out for you:

```bash
# ⚠ EXPORT first. `NEW_PASS=...` makes a SHELL variable; `os.environ` only sees
#   EXPORTED ones, so python raises KeyError, curl posts an empty body and the
#   API answers 422 — which reads like a rejected credential rather than a
#   variable that never reached the process.
export NEW_USER NEW_PASS
echo "sanity: user=$NEW_USER pass-len=${#NEW_PASS}"   # pass-len 0 => not set

python3 -c 'import json,os;print(json.dumps({"login":os.environ["NEW_USER"],"password":os.environ["NEW_PASS"]}))' \
 | curl -fsS -X POST https://app.thebetterdecision.com/api/v1/auth/login \
     -H 'Content-Type: application/json' --data @-
```

An `access_token` in the response is the only proof the hash round-tripped.

⚠ The login body field is **`login`**, not `username` — `LoginRequest` is
`{login, password}` (`backend/app/schemas/auth.py:37-39`), and it accepts a
username OR an email. Sending `username` omits a required field, so the API
answers **422**, which reads like a rejected credential but is the schema
refusing the request before any password is checked. A genuinely wrong password
returns 401. `scripts/smoke-test.sh` is the authoritative example of this call.

⚠ Run this from the host that generated the password, not from the droplet —
the variables live wherever step 1 ran.

⚠ Set a **real** email while you are in there. That is what stops this recurring:
with a reachable address the account is recoverable through `forgot-password`
next time, and none of this is needed again.

⚠ `username` is `String(64) UNIQUE` and the API enforces `^[a-zA-Z0-9._-]+$`,
3-64 chars. SQL bypasses that check, so pick a name that satisfies it or the
next `PUT /users/me` on that row will fail validation. `email` is
`String(120) UNIQUE`.

⚠⚠ **Order matters.** Renaming changes what the founder-count exclusion list must
contain, and changing the password invalidates the session you are using — so
rename first, then rotate, then re-point the secrets.

```bash
BASE=https://app.thebetterdecision.com
# 1. Log in as the CURRENT smoke account.
TOKEN=$(curl -fsS -X POST "$BASE/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"login\":\"$OLD_USER\",\"password\":\"$OLD_PASS\"}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

# 2. Rename it (PUT /users/me; uniqueness and the username rule are enforced).
curl -fsS -X PUT "$BASE/api/v1/users/me" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$NEW_USER\"}"

# 3. Rotate the password. ⚠ 5/hour rate limit; this invalidates the token above.
curl -fsS -X POST "$BASE/api/v1/users/me/password" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"current_password\":\"$OLD_PASS\",\"new_password\":\"$NEW_PASS\"}"

# 4. Re-point the CI secrets.
gh secret set SMOKE_USERNAME --body "$NEW_USER"
gh secret set SMOKE_PASSWORD --body "$NEW_PASS"
```

Then update the founder-count exclusion list to the new name, or
`/api/v1/public/founder-count` counts the smoke account and advertises a number
one too high. It is not silent — `public_stats` logs
`public.founder_count.no_exclusions` at ERROR on every production request while
the list is empty — but it is wrong until fixed.

⚠⚠ **`FOUNDER_COUNT_EXCLUDE_USERNAMES` must be an App Platform SECRET, and the
committed spec must carry its `EV[...]` ciphertext BEFORE the next deploy.** The
committed `.do/app.yaml` is pushed as authoritative on every deploy, so a value
set only in the DO console is erased by the next merge that ships (TBD-425).
Set it in the console, read the blob back with `doctl apps spec get <APP_ID>`,
commit it, and only then deploy.

### How to verify a rollout

1. Watch the Release run: `https://github.com/fjcloudaiconsulting/tbd/actions/workflows/release.yml`
2. Follow the aws-infra bump PR and the Flux apply (runbook above); `kubectl -n tbd-prod logs deploy/backend -c migrate` shows the structured `migrate.*` JSON events.
3. Inspect the running app: `curl -fsS https://app.thebetterdecision.com/health`, `curl -fsS https://app.thebetterdecision.com/ready`, and `curl -fsS https://app.thebetterdecision.com/health/dependencies`.
   `/ready` is the database-only rotation gate; `/health/dependencies` is the one that also covers Redis, and therefore the one that tells you whether anybody can log in.

## 4. Manual deploy escape hatch (`deploy.yml`)

Source: `.github/workflows/deploy.yml`.

⚠ Archived with DigitalOcean since the 2026-10-04 k3s cutover: its `DIGITALOCEAN_ACCESS_TOKEN` was replaced on purpose, so a run fails at `doctl`, and `release.yml` no longer has a `deploy` job. A DigitalOcean rollback is a DNS revert to the still-running DO app, not a run of this workflow. INFRA-44 deletes it after INFRA-49.

`deploy.yml` pushes `.do/app.yaml` to DO App Platform, then runs the smoke tests. It is triggered exclusively by `workflow_dispatch`:

```bash
gh workflow run deploy.yml --ref main
```

When to use it:
- An infra-only commit shipped (`chore(.do): ...`, `chore(nginx): ...`, `chore(Dockerfile): ...`). Run `deploy.yml` to push the new spec.
- Forcing a redeploy of the current `main` (e.g. to refresh credentials surfaced via the spec).

What it does: `app_action/deploy@v2` with `.do/app.yaml`, the PRE_DEPLOY migrate, then the smoke tests. Auth is the `DIGITALOCEAN_ACCESS_TOKEN` secret.

What it does NOT do: bump a version, create a tag, or post to a release feed. It is a deploy-only escape hatch.

For rollback to a prior `main` SHA, see Section 10.

## 5. Apex landing deploy (`apex-deploy.yml`)

The apex landing (`thebetterdecision.com`) is a Next.js static export (`frontend/scripts/build-apex.sh` produces `frontend/out-apex/`) served by the Cloudflare Worker `tbd-landing` (code in `frontend/apex-worker/`). `www` is a proxied Cloudflare record that a redirect rule 301s to the apex (zone managed in aws-infra `terraform/cloudflare`).

The workflow runs on every push to `main` whose paths match the filter at the top of `.github/workflows/apex-deploy.yml` (and on `workflow_dispatch`). The `deploy-worker` job builds the export and runs `wrangler deploy`. It needs one secret, `CLOUDFLARE_API_TOKEN`, in the `landing` environment (deployment branches: `main` only), and skips with a notice while it is unset. No AWS credentials or repository variables are involved.

Shared paths (`frontend/lib/brand.ts`, `frontend/public/**`, `frontend/package.json`, etc.) are also built by `release.yml`, so a change to any of them legitimately fires both pipelines. Landing-only paths only fire `apex-deploy.yml`.

### How to verify an apex deploy

1. Watch the workflow run: `https://github.com/flamarion/pfv/actions/workflows/apex-deploy.yml`
2. Confirm the deployed commit SHA: `curl -fsS https://thebetterdecision.com/_meta.json`

Rollback is in Section 10.

## 6. Terraform: `<tfc-org>/<data-workspace>` (DO data droplet)

Source: `infra/terraform/`, `infra/terraform/README.md`, `infra/README.md`.

This TFC workspace manages the DigitalOcean control plane for the self-hosted MySQL + Redis pair:

| Resource | Purpose |
|---|---|
| `digitalocean_vpc` | Dedicated `<vpc-cidr>` VPC in `<region>` |
| `digitalocean_droplet` | `<data-droplet>`, `<droplet-size>`, Ubuntu 24.04, runs MySQL 8 + Redis |
| `digitalocean_firewall` | SSH 22 from anywhere; MySQL 3306 / Redis 6379 / ICMP from VPC only |
| `digitalocean_project_resources` | Attaches the droplet to the existing DO `pfv` project |

### Workflow

```mermaid
flowchart LR
  pr[PR touches infra/terraform/**] --> tfcsp[TFC speculative plan]
  tfcsp -->|status check on PR| pr
  pr --> merge[Merge to main]
  merge --> tfcapp[TFC apply run created]
  tfcapp --> hold[Status: waiting for confirmation]
  hold -->|operator clicks Confirm and Apply| apply[Apply runs]
  apply --> done[Droplet/VPC/firewall updated]
  apply -.->|outputs| read[droplet_private_ipv4<br/>vpc_id<br/>droplet_public_ipv4]
```

- **Speculative plan** on every PR that touches `infra/terraform/**`. Posted as a PR status check from TFC's VCS integration. Working directory is `infra/terraform`, trigger pattern is `infra/terraform/**`.
- **Apply** on merge. **Manual confirm** in the TFC UI; auto-apply is intentionally off. No infra change ever lands without an operator clicking Confirm & Apply.
- **Local CLI** is debug-only per the `feedback_terraform_vcs_only` rule. `terraform login` once, then `terraform -chdir=infra/terraform plan` reaches the same remote state for inspection. Never `apply` from CLI.

Workspace variables (set in TFC, never committed):
- `do_token` (sensitive): scoped DO API token (droplets/vpcs/firewalls/projects RW, ssh_keys R)
- `ssh_key_name`: name of an SSH key already registered in DO

The provider lock file (`.terraform.lock.hcl`) is committed. TFC and laptop CLIs resolve identical provider versions.

Outputs consumed elsewhere:
- `droplet_private_ipv4` -> `DATABASE_URL` / `REDIS_URL` in `.do/app.yaml`
- `vpc_id` -> top-level `vpc.id` block in `.do/app.yaml` (required for App Platform to reach the droplet on its private IP)

After-droplet steps (one-time): Ansible playbook bootstraps the host. See `infra/README.md`.

## 7. Terraform: apex (removed)

The AWS apex stack (S3, CloudFront, ACM, IAM, Route 53; workspace `tbd-apex`) was removed with INFRA-62. The apex is the Cloudflare Worker `tbd-landing` (Section 5); DNS lives in aws-infra `terraform/cloudflare`.

## 8. Database migrations

`backend/Dockerfile` is multi-stage with two named targets, both built from a venv made in a separate `builder` stage:

| Target | Runs | Build |
|---|---|---|
| `prod` (last stage, so the default) | uvicorn on :8000 | `docker build --target prod -t tbd-backend backend` |
| `migrations` | `python /app/scripts/migrate.py`, one-shot, exits 0/non-zero | `docker build --target migrations -t tbd-migrations backend` |

Both need `DATABASE_URL` and the usual app secrets at run time. `INSTALL_DEV=true` (local compose) adds pytest to the venv.

Three callers, one engine.

```mermaid
sequenceDiagram
  participant Dev as Local dev (backend lifespan)
  participant CLI as ./tbd migrate (local CLI)
  participant DO as DO PRE_DEPLOY job
  participant Wrap as backend/scripts/migrate.py
  participant Alembic as alembic upgrade <rev>
  participant DB as MySQL 8

  Dev->>Dev: read /app/.git/HEAD, refuse off-main unless PFV_MIGRATE_OK_OFF_MAIN=1
  Dev->>Wrap: _run_migrations() (in-process import)
  CLI->>CLI: same branch guard
  CLI->>Wrap: python backend/scripts/migrate.py
  DO->>Wrap: python /app/scripts/migrate.py (no branch guard, always head)
  Wrap->>Alembic: ScriptDirectory.get_heads()
  alt multi-head
    Wrap-->>Wrap: log migrate.failed reason=multiple_heads, exit 1
  else no heads
    Wrap-->>Wrap: log migrate.no_op, exit 0
  else single head
    Wrap->>DB: MigrationContext.get_current_revision()
    alt current == head
      Wrap-->>Wrap: log migrate.no_op, exit 0
    else pending
      Wrap-->>Wrap: log migrate.start (from, to, step_count)
      loop each pending rev (oldest first)
        Wrap-->>Wrap: log migrate.step.start
        Wrap->>Alembic: subprocess alembic upgrade <rev>
        Alembic->>DB: apply migration
        Alembic-->>Wrap: rc 0 + streamed stdout/stderr
        Wrap-->>Wrap: log migrate.step.end (duration_ms, returncode)
      end
      Wrap-->>Wrap: log migrate.complete (applied_count, duration_ms)
    end
  end
```

### The three callers

1. **Local dev (backend lifespan)**: `./tbd start | restart | rebuild` boots the backend. Its FastAPI lifespan calls `_run_migrations()` against the shared MySQL volume in dev. The lifespan reads `/app/.git/HEAD` and **refuses to migrate when the host checkout is on a non-main branch** (or is detached / unreadable). Override with `PFV_MIGRATE_OK_OFF_MAIN=1` in `.env` or the shell.
2. **`./tbd migrate` (local CLI)**: same branch guard. Runs inside the local backend container. Never invoke from an agent worktree (it has no `-p` flag and targets the checkout's default compose project, `tbd` in the main checkout). See `reference_shared_mysql_volume_trap.md`.
3. **Production (DO App Platform `PRE_DEPLOY` job)**: declared in `.do/app.yaml`, runs `python /app/scripts/migrate.py`. The new revision is held back until this job exits 0. The same wrapper is also used by the `migrate` service in `docker-compose.prod.yml`.

### What the wrapper guarantees

- Same exit code semantics as `alembic upgrade head` (0 on success, alembic's exit code on failure, 1 on safety errors). PRE_DEPLOY contract preserved.
- Same stdout / stderr from alembic, line-buffered through threaded forwarders. No capture, no reorder.
- **Multi-head guard**: if `ScriptDirectory.get_heads()` returns >1, the wrapper logs `migrate.failed reason="multiple_heads"` and exits 1. Refuses to auto-pick.
- **Per-step structured JSON events**: an operator triaging from logs alone can answer "did the migrate job do anything, and if so what?":
  - `migrate.start` (from_revision, to_revision, step_count, dialect, database)
  - `migrate.step.start` (revision, step_index, step_count, description)
  - `migrate.step.end` (revision, duration_ms, returncode=0)
  - `migrate.complete` (from_revision, to_revision, applied_count, duration_ms)
  - `migrate.no_op` (when current already equals head)
  - `migrate.failed` (revision, step_index, returncode, reason, error_type)
- Redaction: never logs raw connection URLs (driver errors routinely embed credentials). Only `dialect` and `database` name from `safe_url_fields`.

### Migration policy

- **Forward-only in production.** `alembic downgrade` is forbidden in agent contexts per `feedback_agent_destructive_db_ops`. Rollback path is "write a new fix-up migration" (see Section 10).
- Migrations land via the same PR that uses them. The PRE_DEPLOY job applies them on the next prod deploy, **before** any backend replica with the new code starts.

For env var detail (`DATABASE_URL`, `APP_ENV`, etc.) on the migrate job, see [`ENVIRONMENT.md`](ENVIRONMENT.md) "Migrate job (DO PRE_DEPLOY)". For the managed-to-droplet data move, see [`infra/MIGRATION.md`](../../infra/MIGRATION.md).

## 9. What triggers what (decision tree)

⚠ **`release.yml` has NO `paths:` filter (TBD-424, 2026-08-20).** Every push to
`main` starts a Release run, whatever it touched — a README-only merge included.
What a run then *does* is decided further down the pipe, in two steps:

1. **release-please decides what goes into the release PR**, from the merged
   commit's conventional-commit type (see `release-please-config.json`). It
   only opens or updates the PR; it never tags on an ordinary merge.
2. **`release_created` decides whether images are promoted.** It is set only on
   the merge of the release PR. Any other merge means `promote` and
   `release-smoke` are skipped. Nothing reaches production until the Renovate
   bump PR in aws-infra is merged.

So the common outcome for a non-shipping merge is now a Release run that
concludes in about a minute having done nothing, rather than no run at all.
That is deliberate: the previous path filter answered "should we ship?" from
file paths, which cannot distinguish `chore(frontend):` from `feat(frontend):`,
and silently folded a filtered-out merge's commits into whatever merge next
touched an allowlisted path.

```mermaid
flowchart TD
  start[Commit lands on main with type <type> touching path P]
  start --> rel[release.yml ALWAYS fires: no paths filter]
  rel --> semrel{Is this the merge of the release PR?}
  semrel -- "yes: release_created" --> promote[promote retags images as vX.Y.Z, release-smoke boots them]
  promote --> bump[Renovate bump PR in aws-infra; merging it deploys]
  semrel -- "no: ordinary merge" --> noship[Release PR opened or updated. No tag, no images retagged.]

  start --> apexq{P in the apex allowlist?<br/>app/page.tsx, app/privacy/**,<br/>app/terms/**, app/docs/**,<br/>components/landing/**, lib/brand.ts,<br/>globals.css, build-apex.sh, ...}
  apexq -- yes --> apex[apex-deploy.yml also fires: deploys the tbd-landing Worker]
  apexq -- no --> apexno[apex-deploy.yml does not fire]

  start --> tf2{P in infra/terraform/**?}
  tf2 -- yes --> tfpfv[TFC data workspace apply waits on Confirm and Apply]
```

Backups Terraform changes are made in the aws-infra repo.

Concrete cases:

| You changed | Fires |
|---|---|
| `backend/app/routers/transactions.py` (feat) | `release.yml` updates the release PR; on its merge: release -> promote -> release-smoke. Production rolls when the aws-infra bump PR is merged (migrate init container runs first, no-op if no new revs) |
| `frontend/components/dashboard/Foo.tsx` (feat) | Same path; the frontend image rolls with the bump PR |
| `frontend/app/page.tsx` (feat, landing) | `apex-deploy.yml` deploys the landing. `release.yml` **also runs** and updates the release PR. |
| `frontend/lib/brand.ts` (feat) | Both `release.yml` AND `apex-deploy.yml`. |
| `backend/alembic/versions/abc_new_migration.py` | release PR merge -> promote -> aws-infra bump PR merge -> `migrate` init container applies it -> backend starts |
| `infra/terraform/main.tf` | TFC `<data-workspace>` speculative plan on PR; apply waits on operator Confirm & Apply after merge. `release.yml` runs and only updates the release PR. |
| `.do/app.yaml` (chore) | `release.yml` fires but the change does not enter the release PR. Operator must run `gh workflow run deploy.yml --ref main`. |
| `.github/workflows/test.yml` | `test.yml` triggers itself (it has no paths filter either). On merge, `release.yml` runs and only updates the release PR. |
| `README.md` only | `release.yml` **runs** and only updates the release PR. Nothing is tagged. |

⚠ The old "mutually exclusive apex / DO path-filter split" is **gone on the DO
side**. A landing-only commit no longer skips `release.yml`; if its commit type
warrants a version, it enters the release PR. That is the correct
behaviour — the version line should reflect what shipped, and a landing change
that is worth a `feat` is worth a version — but it is a behaviour change from
what this section used to describe. `apex-deploy.yml` keeps its own `paths:`
filter. It is now the only hand-maintained path allowlist in the repo, and
it is known to have drifted (`features/`, `compare/`, `vs/`,
`lib/dataPolicy.ts`) — tracked as **TBD-433**.

## 10. Rollback playbook

Forward-only philosophy across the board. "Rollback" means "publish a new state that undoes the bad state", not "revert state in place".

### App (production cluster)

Revert the image-bump PR in aws-infra and merge it; Flux rolls the previous tags back. The `migrate` init container only moves the schema forward, so a rollback across a migration needs a fix-up migration (see "Database migrations" below). To undo the code itself, revert the merge commit here (`git revert -m 1 <merge-sha>`, PR, merge) and ship it through the next release.

### Apex landing (`apex-deploy.yml`)

Revert the merge commit and push to `main`; the path filter re-triggers `apex-deploy.yml`, which redeploys the Worker. Alternatively `wrangler rollback` (or the Workers dashboard -> `tbd-landing` -> Deployments) restores a prior Worker version immediately.

### Terraform (either workspace)

Revert the merge commit in the repo. TFC plans the inverse change on the next merge. Operator clicks Confirm & Apply. State catches up.

For destructive teardown (rare), queue a `Destroy plan` from the TFC workspace UI. Local `terraform destroy` is debug-only.

### Database migrations

Forward-only. **Never `alembic downgrade` in production.** The path to a safe rollback is:

1. Open a PR with a new alembic revision that performs the data and schema fix-up. Conventional title `fix(db): ...`.
2. Merge, then merge the release PR, then the aws-infra bump PR. The `migrate` init container applies the fix-up revision and the backend starts on top.
3. Verify via the new revision's `migrate.step.end` event in `kubectl -n tbd-prod logs deploy/backend -c migrate`.

If a migration **partially applies** and the container exits non-zero, the backend pod never starts and, since the Deployment uses `strategy: Recreate`, the old pod is already gone: the API is down until a fix-up revision or an image revert is rolled out. Diagnose from the streamed alembic output + the `migrate.failed` event (`reason`, `step_index`, `revision`). Fix-up paths:
- Schema state matches a known earlier revision: stamp it (`alembic stamp <rev>`) via a one-shot ops session and ship a new revision that completes the work. Only the operator should do this; agents must not (`feedback_agent_destructive_db_ops`).
- Data corruption: write a fix-up migration; ship that.

## 11. Where to look when something breaks

| Surface | Where the logs live |
|---|---|
| GitHub Actions runs (all workflows) | `https://github.com/flamarion/pfv/actions` |
| `release.yml` runs specifically | `https://github.com/fjcloudaiconsulting/tbd/actions/workflows/release.yml` |
| Production rollout, Flux, backend/frontend logs, `migrate` init container logs | [aws-infra `docs/runbooks.md`](https://github.com/fjcloudaiconsulting/aws-infra/blob/main/docs/runbooks.md), "Follow Flux and rollouts" |
| Release published but not on the cluster | The `release-drift-probe` issue in aws-infra |
| `deploy.yml` runs | `https://github.com/flamarion/pfv/actions/workflows/deploy.yml` |
| `apex-deploy.yml` runs | `https://github.com/flamarion/pfv/actions/workflows/apex-deploy.yml` |
| `test.yml` runs | `https://github.com/flamarion/pfv/actions/workflows/test.yml` |
| TFC `<data-workspace>` (DO data droplet) | `https://app.terraform.io/app/<tfc-org>/workspaces/<data-workspace>` |
| DO App Platform deploys | DO console -> Apps -> `pfv` -> Activity |
| Backend access logs (live) | DO console -> Apps -> `pfv` -> Runtime Logs -> backend component |
| Frontend access logs (live) | DO console -> Apps -> `pfv` -> Runtime Logs -> frontend component |
| `PRE_DEPLOY migrate` job logs | DO console -> Apps -> `pfv` -> Activity -> select deploy -> migrate job |
| Apex Worker logs and versions | Cloudflare dashboard -> Workers & Pages -> `tbd-landing` |
| MySQL slow query / error log | SSH to `<data-droplet>`: `journalctl -u mysql` or `/var/log/mysql/error.log` |
| Nightly mysqldump | `<data-droplet>`: `ls -lh /var/backups/mysql/`; log at `/var/log/mysql-backup.log` |
| Smoke-test failure GitHub issue | Auto-opened by `scripts/notify-smoke-failure.sh`; check open issues in `flamarion/pfv` |

Triage shortcuts:

| Symptom | First look at |
|---|---|
| Merge to `main` happened, prod didn't update | `release.yml` -> did `release` set `release_created=true`? Only the merge of the release PR cuts a release, and production only changes when the aws-infra bump PR is merged |
| `release` job failed after the release PR merged | The Test run of the release PR's commit is red. Re-run that commit's failed Test jobs (not a `workflow_dispatch` run), then re-run the failed Release run (or wait for the next push to `main`) |
| Release created but `promote` or `release-smoke` failed | Re-run the failed jobs of that Release run; the release already exists, so a new push to `main` will not redo them |
| Release published, no bump PR in aws-infra | Renovate, then the `release-drift-probe` issue |
| `release` job red after release-please already published the GitHub Release | `promote` never ran and a re-run cannot recover it (release-please finds the release and reports no `release_created`). Retag that commit's `sha-<7>` images as `vX.Y.Z` by hand, as `promote-release.yml` does; otherwise `release-drift-probe` flags it after its grace days |
| Drift probe reports AHEAD after a release | DO builds `main` HEAD, so a merge that landed between the release and the deploy ships with it; AHEAD clears at the next release |
| Rollout done, app still broken | The `[post-deploy-smoke] tbd-prod` issue in aws-infra, or `scripts/smoke-test.sh` by hand (runbook above), then the backend/frontend pod logs |
| `migrate` init container hung or failed | `kubectl -n tbd-prod logs deploy/backend -c migrate`. Grep for `migrate.start`, `migrate.failed`, `migrate.step.start`. Multi-head? Driver error? |
| Apex site shows stale content | Confirm the `deploy-worker` job of `apex-deploy.yml` ran for the SHA (it skips green while `CLOUDFLARE_API_TOKEN` is unset in the `landing` environment); `curl https://thebetterdecision.com/_meta.json` |
| App can't reach MySQL or Redis | Confirm `.do/app.yaml`'s top-level `vpc.id` matches the TFC output, and `DATABASE_URL` / `REDIS_URL` point at the droplet's `<vpc-cidr>` private IP |
| Secret env var "disappeared" after deploy | `.do/app.yaml` must declare every SECRET with its `EV[...]` blob. Missing -> stripped on push. Refresh via `doctl apps spec get <app-id>` |

For the env var matrix and common per-variable failures (Google SSO button missing, old `NEXT_PUBLIC_*` keys in the live spec, audit log shows ingress IP, etc.), see [`ENVIRONMENT.md`](ENVIRONMENT.md) "Common failure modes".
