# DEPLOYMENT.md

Audience: a contributor who just cloned the repo and wants to understand what happens between `git push` and a live change at `app.thebetterdecision.com` or `thebetterdecision.com`. Also a triage reference for CI/CD failures.

All five pipelines described here are live on `main` today (`test.yml`, `release.yml`, `deploy.yml`, `apex-deploy.yml`, `test-durations.yml`). The apex landing is public at `https://thebetterdecision.com` (and `https://www.thebetterdecision.com`, which 301-redirects to the apex).

For "how do I get my code ready to push", read [`CONTRIBUTING.md`](./CONTRIBUTING.md). For the env var matrix, read [`ENVIRONMENT.md`](./ENVIRONMENT.md). For the managed-to-droplet data move, read [`infra/MIGRATION.md`](./infra/MIGRATION.md). This file does not duplicate any of them.

## 1. Overview

Four production surfaces. Each has its own pipeline. Some changes fan out across more than one.

| Surface | URL | Hosted by | Updated by |
|---|---|---|---|
| App (FastAPI + Next.js dashboard) | `https://app.thebetterdecision.com` | DigitalOcean App Platform (`pfv` app) | `release.yml` (auto) or `deploy.yml` (manual) |
| Apex landing (marketing, privacy, terms, docs) | `https://thebetterdecision.com` | AWS S3 + CloudFront | `apex-deploy.yml` (auto) |
| Data plane (MySQL 8 + Redis) | private VPC IP `<vpc-ip>:3306 / :6379` | Self-hosted DO droplet `<data-droplet>` | TFC workspace `<tfc-org>/<data-workspace>` (manual confirm) |
| Apex CDN + cert + IAM | n/a (control plane) | AWS (S3, CloudFront, ACM, IAM, Route 53) | TFC workspace `<tfc-org>/<apex-workspace>` (manual confirm) |

```mermaid
flowchart LR
  dev[Contributor push to main] --> rel[Release workflow]
  dev --> apex[Apex Deploy workflow]
  dev --> tfc1[TFC data workspace]
  dev --> tfc2[TFC apex workspace]

  rel -->|semantic-release published| do[DO App Platform pfv]
  do --> appurl[app.thebetterdecision.com]

  apex -->|build + S3 sync + CF invalidate| s3[AWS S3]
  s3 --> cf[CloudFront]
  cf --> apexurl[thebetterdecision.com]

  tfc1 -->|Confirm and Apply| droplet[<data-droplet> droplet]
  droplet --> db[MySQL 8 + Redis]
  do -.->|VPC private IP| db

  tfc2 -->|Confirm and Apply| awsctl[S3 + CloudFront + ACM + IAM + Route53]
  awsctl -.- s3
  awsctl -.- cf

  classDef pipe fill:#eef,stroke:#446
  class rel,apex,tfc1,tfc2 pipe
```

The data plane is reached by App Platform over the VPC's private IPv4. The apex CDN bucket is reached by GitHub Actions over the public AWS API using OIDC-issued credentials, not long-lived keys.

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
| **Backend Checks** | Python 3.12, `pip install -r backend/requirements-dev.txt`, `pytest`, then `python -m compileall backend/app` | Pytest failed, or a syntax error slipped in that pytest didn't reach |
| **Frontend Checks** | Node 22, `npm ci`, `scripts/check-design-tokens.sh`, `npm run lint -- --quiet`, `npm test`, `npm run build` | One of: design-token violation, lint error, test failure, production build failure |

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
  fe --> fei[npm ci]
  fe --> fed[check-design-tokens.sh]
  fe --> fel[npm run lint --quiet]
  fe --> fet[npm test]
  fe --> feb[npm run build]
  bep & bec & fed & fel & fet & feb --> merge[Both jobs green = mergeable]
```

How to read failures:
- **Design tokens**: `frontend/scripts/check-design-tokens.sh` scans for hard-coded colors / spacings that should use brand tokens. Output names the file and line.
- **Lint**: `npm run lint -- --quiet` shows only errors (warnings are tolerated; treat warnings shown in logs as informational).
- **Frontend build**: a build failure here means it will also fail in production. Test locally with `docker compose exec frontend npm run build`.
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

## 3. Backend + Frontend production deploy (`release.yml`)

Source: `.github/workflows/release.yml`. Spec: `.do/app.yaml`.

`release.yml` is the **single arbiter** of "should we ship to prod". It runs on every push to `main` whose changed paths intersect a coarse allowlist, and uses **semantic-release** to decide whether the merge warrants a new version. If yes, the gated `deploy` job pushes `.do/app.yaml` to DO App Platform; DO runs the `PRE_DEPLOY` migrate job, then rolls the backend and frontend services; then `smoke-tests` asserts the live app actually serves traffic.

### Trigger

```yaml
on:
  push:
    branches: [main]
    paths:
      - "backend/**"
      - "frontend/**"
      - "nginx/**"
      - ".do/**"
      - "Dockerfile*"
```

This path filter is a **coarse precheck**, not the deploy gate. Pushes that don't touch any allowlisted path skip the workflow entirely (no CI minutes spent). Pushes that do touch one still run, and then semantic-release inside the workflow makes the real ship/no-ship call based on conventional commit types (`feat:`, `fix:`, etc).

Why this design: pre-PR #178 the workflow shipped on every allowlisted change, which meant a `chore(frontend): tsconfig` merge would redeploy production for no reason. Semantic-release suppresses `chore:`, `perf:`, `docs:`, `refactor:`, etc. and only ships when a `feat:` or `fix:` (or higher) gets merged.

### Job graph

```mermaid
sequenceDiagram
  participant Owner
  participant GH as GitHub Actions
  participant SR as semantic-release
  participant DO as DO App Platform
  participant MIG as PRE_DEPLOY migrate job
  participant BE as backend service
  participant FE as frontend service
  participant SMK as smoke-tests job

  Owner->>GH: merge PR to main (path-filtered)
  GH->>SR: run release job
  SR->>SR: analyze conventional commits since last tag
  alt new_release_published == true
    SR->>GH: tag v.X.Y.Z, GitHub Release
    GH->>DO: deploy job: app_action/deploy@v2 with .do/app.yaml
    DO->>MIG: kind: PRE_DEPLOY, run python /app/scripts/migrate.py
    MIG-->>DO: rc 0, alembic head reached
    DO->>BE: roll backend (health probe /health)
    DO->>FE: roll frontend (health probe /health)
    BE-->>DO: 200 OK
    FE-->>DO: 200 OK
    DO-->>GH: deploy ACTIVE
    GH->>SMK: smoke-tests job (needs: deploy)
    SMK->>SMK: scripts/smoke-test.sh against app.thebetterdecision.com
    alt smoke fails
      SMK->>GH: open or comment on a GH issue (notify-smoke-failure.sh)
    end
  else no_release
    SR-->>GH: skip deploy and smoke-tests
  end
```

### The gate

```yaml
deploy:
  needs: release
  if: needs.release.outputs.new_release_published == 'true'
```

This is the load-bearing line. Without `new_release_published`, `deploy` does not run, `smoke-tests` does not run, nothing ships. The output is set by `cycjimmy/semantic-release-action@v6` based on whether the conventional-commit analysis produced a new version.

### Why a gated `deploy` job and not `on: release: { types: [published] }`

GitHub does not cascade workflow runs when a release is created by `GITHUB_TOKEN`, which is what semantic-release uses. A separate workflow listening on `release.published` would never fire. So `deploy` lives in the same workflow file and gates on the upstream job's output.

### Deploy step internals

`digitalocean/app_action/deploy@v2` with `app_spec_location: .do/app.yaml` and **no `app_name`**. With both set, the action ignores the file and fetches the live spec by name (that bug let PR #79's migration sit un-applied for hours). The action reads `.do/app.yaml` and picks up the app via its top-level `name: pfv` field. The current spec drives every deploy, which means:

- Every SECRET must be declared in `.do/app.yaml` with its encrypted `EV[...]` value, or it gets removed from the live app on push. (See ENVIRONMENT.md "Spec-sync hazards" and the file's own preamble comment.)
- Every domain, env var, ingress rule, and component must be present and current in the file.

### `PRE_DEPLOY` migrate job

Declared in `.do/app.yaml` as:

```yaml
jobs:
  - name: migrate
    kind: PRE_DEPLOY
    source_dir: backend
    dockerfile_path: backend/Dockerfile
    run_command: python /app/scripts/migrate.py
```

App Platform holds the new revision back until this job exits 0. A long migration never causes the backend's serving probe to fail because the backend doesn't start serving until migrate is done. See Section 8 for migration details.

### Smoke tests

`scripts/smoke-test.sh` runs after `deploy` succeeds. Env: `SMOKE_BASE_URL=https://app.thebetterdecision.com`, plus a `SMOKE_USERNAME` / `SMOKE_PASSWORD` for a dedicated smoke user. The smoke user must exist, must be `email_verified`, and must **not** have MFA enabled.

On failure, `scripts/notify-smoke-failure.sh` opens (or comments on an existing) GitHub issue using `GH_TOKEN`. DO marking the deploy `ACTIVE` is necessary but not sufficient: smoke tests assert end-to-end traffic actually works.

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
3. **No elevated rights.** It reads `GET /api/v1/categories` and nothing more.
   It must never be a superadmin or hold an org role beyond the minimum.

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
python3 -c 'import json,os;print(json.dumps({"username":"<new-name>","password":os.environ["NEW_PASS"]}))' \
 | curl -fsS -X POST https://app.thebetterdecision.com/api/v1/auth/login \
     -H 'Content-Type: application/json' --data @-
```

An `access_token` in the response is the only proof the hash round-tripped.

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
  -d "{\"username\":\"$OLD_USER\",\"password\":\"$OLD_PASS\"}" \
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

### How to verify a deploy

1. Watch the workflow run: `https://github.com/flamarion/pfv/actions/workflows/release.yml`
2. Watch the DO deploy: DO console -> Apps -> `pfv` -> Activity. The PRE_DEPLOY job logs appear first; structured `migrate.*` JSON events stream there.
3. Inspect the running app: `curl -fsS https://app.thebetterdecision.com/health`, `curl -fsS https://app.thebetterdecision.com/ready`, and `curl -fsS https://app.thebetterdecision.com/health/dependencies`.
   `/ready` is the database-only rotation gate; `/health/dependencies` is the one that also covers Redis, and therefore the one that tells you whether anybody can log in.

## 4. Manual deploy escape hatch (`deploy.yml`)

Source: `.github/workflows/deploy.yml`.

`deploy.yml` mirrors `release.yml`'s `deploy` + `smoke-tests` jobs. It is triggered exclusively by `workflow_dispatch`:

```bash
gh workflow run deploy.yml --ref main
```

When to use it:
- An infra-only commit shipped (`chore(.do): ...`, `chore(nginx): ...`, `chore(Dockerfile): ...`). semantic-release will not bump for `chore:`, so `release.yml` will not deploy. Run `deploy.yml` to push the new spec.
- The most recent `release.yml` run failed at the deploy step and the cause is fixed, but no new release will be cut from a follow-up commit.
- Forcing a redeploy of the current `main` (e.g. to refresh credentials surfaced via the spec).

What it does: same `app_action/deploy@v2` + `.do/app.yaml`, same PRE_DEPLOY migrate, same smoke tests. Auth is the same `DIGITALOCEAN_ACCESS_TOKEN` secret.

What it does NOT do: bump a version, create a tag, or post to a release feed. It is a deploy-only escape hatch.

For rollback to a prior `main` SHA, see Section 10.

## 5. Apex landing deploy (`apex-deploy.yml`)

> Workflow is live on `main` (shipped in PR #267). It deploys to S3 + CloudFront on every push to `main` whose paths match the filter below. Public traffic reaches the distribution via the apex / www ALIAS records provisioned by PR #270.

The apex landing (`thebetterdecision.com`) is a Next.js static export. `frontend/scripts/build-apex.sh` produces `frontend/out-apex/`. The workflow uploads that directory to an S3 bucket fronted by CloudFront in AWS, using **GitHub OIDC** to assume an IAM role (no long-lived AWS keys committed anywhere). The bucket, distribution, ACM cert, and IAM roles are provisioned by the `<tfc-org>/<apex-workspace>` TFC workspace (Section 7).

### Trigger and path filter

```yaml
on:
  push:
    branches: [main]
    paths:
      # Landing surface
      - "frontend/app/page.tsx"
      - "frontend/app/privacy/**"
      - "frontend/app/terms/**"
      - "frontend/app/docs/**"
      # Structural retained app files
      - "frontend/app/layout.tsx"
      - "frontend/app/not-found.tsx"
      - "frontend/app/error.tsx"
      - "frontend/app/loading.tsx"
      - "frontend/app/global-error.tsx"
      - "frontend/app/icon.svg"
      - "frontend/app/globals.css"
      # Landing components and apex-build helpers
      - "frontend/components/landing/**"
      - "frontend/components/auth/AuthProviderApex.tsx"
      - "frontend/scripts/build-apex.sh"
      - "frontend/next.config.apex.ts"
      - "frontend/lib/links.ts"
      # Shared brand and styling
      - "frontend/lib/brand.ts"
      - "frontend/lib/site.ts"
      - "frontend/lib/styles.ts"
      - "frontend/components/brand/**"
      - "frontend/components/ThemeProvider.tsx"
      - "frontend/components/tour/**"
      - "frontend/components/ui/BackLink.tsx"
      - "frontend/components/ui/CurrentYear.tsx"
      - "frontend/components/ui/ThemeToggle.tsx"
      - "frontend/public/**"
      # Build inputs
      - "frontend/package.json"
      - "frontend/package-lock.json"
      # The workflow itself
      - ".github/workflows/apex-deploy.yml"
```

Shared paths (`frontend/lib/brand.ts`, `frontend/lib/styles.ts`, `frontend/components/ThemeProvider.tsx`, `frontend/public/**`, `frontend/package.json`, etc.) are also on `release.yml`'s allowlist. A change to any of these legitimately affects both surfaces and triggers both pipelines.

Landing-only paths (`frontend/app/page.tsx`, `frontend/app/privacy/**`, `frontend/components/landing/**`, etc.) are **not** on `release.yml`'s allowlist, so a landing-only commit skips the DO release entirely. That is the intended split.

### Permissions and concurrency

```yaml
permissions:
  contents: read
  id-token: write   # required for OIDC to AWS

concurrency:
  group: apex-deploy
  cancel-in-progress: false   # never cancel an in-flight S3 sync
```

### Repository variables (Settings -> Secrets and variables -> Actions -> Variables tab)

| Variable | Source | Purpose |
|---|---|---|
| `AWS_APEX_DEPLOY_ROLE_ARN` | TFC output `github_actions_role_arn` | Role assumed via OIDC |
| `AWS_APEX_BUCKET` | TFC output `s3_bucket_name` | S3 sync target |
| `AWS_APEX_DISTRIBUTION_ID` | TFC output `cloudfront_distribution_id` | CloudFront invalidation target |
| `AWS_APEX_REGION` (optional) | n/a | Defaults to `eu-central-1` in the workflow |

No secrets are needed. These are public-shaped identifiers.

### Pipeline

```mermaid
sequenceDiagram
  participant GH as GitHub Actions
  participant Node as Node 22 runner
  participant OIDC as GitHub OIDC -> AWS
  participant S3 as S3 bucket (apex)
  participant CF as CloudFront distribution

  GH->>Node: actions/checkout, setup-node@22
  Node->>Node: npm ci --no-audit --no-fund (frontend/)
  Node->>Node: npm run build:apex (build-apex.sh, NEXT_PUBLIC_BUILD_TARGET=apex)
  Node->>Node: verify out-apex/index.html and _meta.json exist
  Node->>OIDC: aws-actions/configure-aws-credentials@v4 (role-to-assume)
  OIDC-->>Node: short-lived STS creds (sub claim must match repo:flamarion/pfv:ref:refs/heads/main)
  Note over Node,S3: Order matters, see comments
  Node->>S3: aws s3 sync out-apex/_next/static/ s3://BUCKET/_next/static/<br/>NO --delete, cache-control max-age=31536000 immutable
  Node->>S3: aws s3 sync out-apex/ s3://BUCKET/<br/>--delete --exclude "_next/static/*"<br/>cache-control max-age=300 s-maxage=3600
  Node->>S3: aws s3 cp out-apex/_meta.json s3://BUCKET/_meta.json<br/>cache-control no-cache no-store must-revalidate, content-type application/json
  Node->>CF: aws cloudfront create-invalidation --paths "/*"
  CF-->>Node: invalidation id
  Node->>GH: print summary (commit, bucket, distribution, verify URL)
```

### Why the sync order is load-bearing

1. **Immutable-first NO `--delete`**: hashed chunks under `_next/static/**` are content-addressed by Next.js. Locked at `max-age=31536000, immutable`. The newly-published HTML references new hashed-asset URLs; those chunks must exist in S3 **before** the HTML is visible to viewers.
2. **Then mutable WITH `--delete`**: everything outside `_next/static/**` (HTML, icons, OG/apple images, fonts, JSON, txt, xml) gets short-cached and prunes deleted objects. Browser-cached old HTML (5-min TTL) still references old hashed-asset URLs; deleting those mid-flight would produce 404s for users who have not yet refetched the HTML.
3. **Override `_meta.json` last**: `_meta.json` is the deploy-verification probe. It is re-uploaded with `Cache-Control: no-cache, no-store, must-revalidate` so a curl against the apex always returns the freshest SHA.
4. **`/*` invalidation**: blanket. CloudFront invalidations are pennies per path; the simpler invariant beats the cost optimization.

Trade-off: orphaned hashed chunks accumulate. PR #240's S3 lifecycle policy prunes noncurrent versions after 90 days, but **not** orphaned-by-rename objects. Periodic cleanup is a tracked follow-up.

### Auth boundary

The OIDC trust policy on `github-actions-apex-deploy` (provisioned by PR #240) uses `StringEquals` on the OIDC `sub` claim, pinned to exactly `repo:flamarion/pfv:ref:refs/heads/main`. PR-context tokens have a different `sub` and are rejected at the IAM trust level. Workflow `if:` guards alone would be insufficient since PR authors can edit the workflow file. The `branches: [main]` trigger is belt-and-suspenders.

### How to verify an apex deploy

1. Watch the workflow run: `https://github.com/flamarion/pfv/actions/workflows/apex-deploy.yml`
2. Confirm the deployed commit SHA via `/_meta.json` (object is `no-cache`, so the response is always fresh):
   ```bash
   curl -fsS https://thebetterdecision.com/_meta.json
   ```
   If the apex hostname is unreachable for any reason (incident, DNS misconfiguration), the same probe works against the CloudFront-assigned hostname from the TFC output `cloudfront_distribution_domain`:
   ```bash
   curl -fsS https://<distribution>.cloudfront.net/_meta.json
   ```
3. CloudFront invalidation status: AWS console -> CloudFront -> Distributions -> select -> Invalidations tab.

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

- **Speculative plan** on every PR that touches `infra/terraform/**`. Posted as a PR status check from TFC's VCS integration. Working directory is `infra/terraform`, trigger pattern is `infra/terraform/**`. (The apex workspace has its own trigger; the two do not collide.)
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

## 7. Terraform: `<tfc-org>/<apex-workspace>` (AWS apex control plane)

Source: `infra/terraform/apex/`, `infra/terraform/apex/README.md`.

Separate TFC workspace because the auth path is different (AWS OIDC rather than a DO API token) and the blast radius is contained.

| Resource | Purpose |
|---|---|
| `aws_s3_bucket` (+ public-access-block, versioning, SSE, lifecycle, ownership) | Private origin bucket for the static export |
| `aws_cloudfront_distribution` (+ OAC) | Edge distribution with HTTPS, HSTS, www -> apex redirect |
| `aws_cloudfront_function` | Viewer-request: www -> apex 301 redirect, then S3 directory-index rewrite (`/privacy/` -> `/privacy/index.html`) |
| `aws_cloudfront_response_headers_policy` | HSTS, X-Frame-Options, Referrer-Policy, Permissions-Policy |
| `aws_acm_certificate` + `_validation` | DNS-validated cert in `us-east-1` for apex + www (CloudFront requirement) |
| `aws_route53_record.apex_acm_validation` | ACM `_<token>` CNAME records in the existing zone (separate from the apex / www A + AAAA ALIAS records, which are also managed by this module). |
| `aws_iam_openid_connect_provider.github` | Trust for GitHub Actions OIDC tokens |
| `aws_iam_openid_connect_provider.tfc` | Trust for Terraform Cloud workload-identity tokens |
| `aws_iam_role.github_actions_apex_deploy` | Deploy role: `s3:PutObject`/`s3:DeleteObject`/`s3:ListBucket` scoped to the apex bucket; `cloudfront:CreateInvalidation` scoped to the apex distribution |
| `aws_iam_role.tfc_apex_provisioner` | TFC-assumed role for managing the resources above |

### Workflow

Same shape as the `pfv` workspace:
- Speculative plan on every PR that touches `infra/terraform/apex/**`
- Apply on merge to `main`, **manual Confirm & Apply** in TFC
- Local CLI plan-only is allowed for debug; never apply from CLI

```mermaid
flowchart LR
  pr[PR touches infra/terraform/apex/**] --> tfcsp[TFC apex speculative plan]
  tfcsp -->|status check on PR| pr
  pr --> merge[Merge to main]
  merge --> tfcapp[TFC apex apply run]
  tfcapp --> hold[Waiting on Confirm and Apply]
  hold --> apply[Apply runs via OIDC -> tfc-apex-provisioner role]
  apply --> done[AWS resources updated]
  apply -.->|outputs| consumers[github_actions_role_arn -> apex-deploy.yml<br/>s3_bucket_name -> apex-deploy.yml<br/>cloudfront_distribution_id -> apex-deploy.yml<br/>cloudfront_distribution_domain -> diagnostic / fallback probe]
```

### Bootstrap (one-time)

Because the OIDC providers and `tfc-apex-provisioner` role only exist after the first apply, the first run uses a single static-credential window:

1. Create IAM user `pfv-apex-bootstrap` with `AdministratorAccess`. Generate an access-key pair.
2. In TFC -> `<tfc-org>/<apex-workspace>` -> Variables: set `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (both env, sensitive), `aws_account_id` (terraform).
3. Merge the apex Terraform PR. Confirm & Apply.
4. Switch TFC to OIDC: set `TFC_AWS_PROVIDER_AUTH=true`, `TFC_AWS_RUN_ROLE_ARN=<tfc_role_arn output>`. Delete `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`.
5. Delete (or deactivate the access key of) the bootstrap IAM user within the hour.
6. Trigger a no-op plan in TFC; an empty plan = OIDC works end-to-end.

After bootstrap, TFC runs use workload identity. Long-lived AWS keys exist nowhere in the repo or in TFC.

### What this workspace does **not** do

- It does **not** manage the dashboard surface (`app.thebetterdecision.com`). That DNS lives on Cloudflare and points at App Platform.
- Route 53 writes are split into two narrowly-scoped IAM statements: `A` and `AAAA` on exactly the apex and www FQDNs, and `CNAME` on the exact ACM validation names exposed by `aws_acm_certificate.apex.domain_validation_options`. Every other record type and every other name in the zone is IAM-blocked.

## 8. Database migrations

Three callers, one engine.

```mermaid
sequenceDiagram
  participant Dev as Local dev (backend lifespan)
  participant CLI as ./pfv migrate (local CLI)
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

1. **Local dev (backend lifespan)**: `./pfv start | restart | rebuild` boots the backend. Its FastAPI lifespan calls `_run_migrations()` against the shared MySQL volume in dev. The lifespan reads `/app/.git/HEAD` and **refuses to migrate when the host checkout is on a non-main branch** (or is detached / unreadable). Override with `PFV_MIGRATE_OK_OFF_MAIN=1` in `.env` or the shell.
2. **`./pfv migrate` (local CLI)**: same branch guard. Runs inside the local backend container. Never invoke from an agent worktree (it always targets the default `pfv` compose project). See `reference_shared_mysql_volume_trap.md`.
3. **Production (DO App Platform `PRE_DEPLOY` job)**: declared in `.do/app.yaml`, runs `python /app/scripts/migrate.py`. The new revision is held back until this job exits 0. The same wrapper is also used by the K8s init container in `k8s/templates/backend.yaml` and by the `migrate` service in `docker-compose.prod.yml`.

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

For env var detail (`DATABASE_URL`, `APP_ENV`, etc.) on the migrate job, see [`ENVIRONMENT.md`](./ENVIRONMENT.md) "Migrate job (DO PRE_DEPLOY)". For the managed-to-droplet data move, see [`infra/MIGRATION.md`](./infra/MIGRATION.md).

## 9. What triggers what (decision tree)

⚠ **`release.yml` has NO `paths:` filter (TBD-424, 2026-08-20).** Every push to
`main` starts a Release run, whatever it touched — a README-only merge included.
What a run then *does* is decided further down the pipe, in two steps:

1. **`.releaserc.json` decides whether a version is cut**, from the merged
   commit's conventional-commit type and scope. `feat` / `fix` / `revert` and
   anything breaking cut one; `chore`, `docs`, `style`, `refactor`, `test`,
   `build`, `ci`, `perf` and the suppressed scopes (`ci`, `deps-dev`, `test`,
   `tests`, `dev`, `infra`) do not. ⚠ Those rules are ORDER-DEPENDENT — see
   `backend/tests/test_release_rules_ordering.py`.
2. **`new_release_published` decides whether it deploys.** No version cut means
   the `deploy` job is skipped and `.do/app.yaml` is never pushed.

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
  rel --> semrel{Does .releaserc.json cut a version?}
  semrel -- "feat / fix / revert / breaking" --> deploy[deploy job pushes .do/app.yaml, then smoke tests]
  semrel -- "chore / docs / ci / perf / suppressed scope" --> noship[Release run completes. No tag, no deploy.]

  start --> apexq{P in the apex allowlist?<br/>app/page.tsx, app/privacy/**,<br/>app/terms/**, app/docs/**,<br/>components/landing/**, lib/brand.ts,<br/>globals.css, build-apex.sh, ...}
  apexq -- yes --> apex[apex-deploy.yml also fires: S3 sync + CloudFront invalidation]
  apexq -- no --> apexno[apex-deploy.yml does not fire]

  start --> tf1{P in infra/terraform/apex/**?}
  tf1 -- yes --> tfapex[TFC apex workspace apply waits on Confirm and Apply]
  tf1 -- no --> tf2{P in infra/terraform/**?}
  tf2 -- yes --> tfpfv[TFC data workspace apply waits on Confirm and Apply]
```

Concrete cases:

| You changed | Fires |
|---|---|
| `backend/app/routers/transactions.py` (feat) | `release.yml` -> semantic-release publishes -> deploy -> migrate (no-op if no new revs) -> roll backend |
| `frontend/components/dashboard/Foo.tsx` (feat) | `release.yml` -> publishes -> deploy -> roll frontend |
| `frontend/app/page.tsx` (feat, landing) | `apex-deploy.yml` deploys the landing. `release.yml` **also runs now** and publishes a version, which redeploys DO. |
| `frontend/lib/brand.ts` (feat) | Both `release.yml` AND `apex-deploy.yml`. |
| `backend/alembic/versions/abc_new_migration.py` | `release.yml` -> deploy -> PRE_DEPLOY migrate applies it -> roll backend |
| `infra/terraform/main.tf` | TFC `<data-workspace>` speculative plan on PR; apply waits on operator Confirm & Apply after merge. `release.yml` runs and no-ops (`chore`/`ci` type, or `infra` scope). |
| `infra/terraform/apex/main.tf` | TFC `<apex-workspace>`; same `release.yml` no-op. |
| `.do/app.yaml` (chore) | `release.yml` fires but semantic-release does not bump. Operator must run `gh workflow run deploy.yml --ref main`. |
| `.github/workflows/test.yml` | `test.yml` triggers itself (it has no paths filter either). On merge, `release.yml` runs and no-ops on the `ci` type. |
| `README.md` or `CLAUDE.md` only | `release.yml` **runs** and no-ops on the `docs` type. Nothing is tagged and nothing deploys. |

⚠ The old "mutually exclusive apex / DO path-filter split" is **gone on the DO
side**. A landing-only commit no longer skips `release.yml`; if its commit type
warrants a version, it cuts one and redeploys DO. That is the correct
behaviour — the version line should reflect what shipped, and a landing change
that is worth a `feat` is worth a version — but it is a behaviour change from
what this section used to describe. `apex-deploy.yml` keeps its own `paths:`
filter, for a cost reason and not a correctness one: every apex run does an S3
sync plus a CloudFront `/*` invalidation, and invalidations past 1,000/month
are metered. It is now the only hand-maintained path allowlist in the repo, and
it is known to have drifted (`features/`, `compare/`, `vs/`,
`lib/dataPolicy.ts`) — tracked as **TBD-433**.

## 10. Rollback playbook

Forward-only philosophy across the board. "Rollback" means "publish a new state that undoes the bad state", not "revert state in place".

### DO App (`release.yml` / `deploy.yml`)

Option A, revert the merge commit:
1. `git revert -m 1 <merge-sha>` on a branch, push, PR, merge.
2. semantic-release sees the revert as a fix or feat (conventional-commit style matters) and publishes a new release.
3. `deploy` ships, PRE_DEPLOY runs (no-op if the revert did not touch migrations), backend / frontend roll.
4. Smoke tests confirm.

Option B, redeploy a prior good `main` SHA via `deploy.yml`:
1. Reset `main` to a prior known-good commit is **not allowed** (PR-only workflow). Instead:
2. Cherry-pick the inverse of the bad change onto a new branch, PR, merge. This is effectively Option A.

DO console "Rollback" button: avoid. App Platform's rollback rolls the **runtime image** back to a prior build, but the **spec** (`.do/app.yaml`) on the next push still reflects whatever is on `main`. This produces a runtime / spec mismatch that triggers more redeploys to resolve. Always rollback via the repo.

### Apex landing (`apex-deploy.yml`)

Option A, revert the merge commit, push to `main`. The path filter re-triggers `apex-deploy.yml`, which rebuilds and re-syncs. CloudFront `/*` invalidation flushes the edge.

Option B, restore prior S3 object versions. The apex bucket has versioning enabled (provisioned by PR-A). For a surgical undo (e.g. a single `privacy/index.html` regression):
```bash
aws s3api list-object-versions --bucket <AWS_APEX_BUCKET> --prefix privacy/index.html
aws s3api copy-object \
  --bucket <AWS_APEX_BUCKET> --key privacy/index.html \
  --copy-source "<AWS_APEX_BUCKET>/privacy/index.html?versionId=<prior-version-id>"
aws cloudfront create-invalidation --distribution-id <AWS_APEX_DISTRIBUTION_ID> --paths "/privacy/index.html" "/privacy/"
```
Prefer Option A for any rollback that affects more than one or two objects; the repo stays the source of truth.

### Terraform (either workspace)

Revert the merge commit in the repo. TFC plans the inverse change on the next merge. Operator clicks Confirm & Apply. State catches up.

For destructive teardown (rare), queue a `Destroy plan` from the TFC workspace UI. Local `terraform destroy` is debug-only.

### Database migrations

Forward-only. **Never `alembic downgrade` in production.** The path to a safe rollback is:

1. Open a PR with a new alembic revision that performs the data and schema fix-up. Conventional title `fix(db): ...`.
2. Merge. `release.yml` -> semantic-release publishes -> deploy -> PRE_DEPLOY migrate applies the fix-up revision. Backend rolls on top.
3. Verify via the new revision's `migrate.step.end` event in the PRE_DEPLOY job logs.

If a migration **partially applies** and the job exits non-zero, the PRE_DEPLOY contract halts the rollout. The new backend revision never starts. Diagnose from the streamed alembic output + the `migrate.failed` event (`reason`, `step_index`, `revision`). Fix-up paths:
- Schema state matches a known earlier revision: stamp it (`alembic stamp <rev>`) via a one-shot ops session and ship a new revision that completes the work. Only the operator should do this; agents must not (`feedback_agent_destructive_db_ops`).
- Data corruption: write a fix-up migration; ship that.

## 11. Where to look when something breaks

| Surface | Where the logs live |
|---|---|
| GitHub Actions runs (all workflows) | `https://github.com/flamarion/pfv/actions` |
| `release.yml` runs specifically | `https://github.com/flamarion/pfv/actions/workflows/release.yml` |
| `deploy.yml` runs | `https://github.com/flamarion/pfv/actions/workflows/deploy.yml` |
| `apex-deploy.yml` runs (post-#267) | `https://github.com/flamarion/pfv/actions/workflows/apex-deploy.yml` |
| `test.yml` runs | `https://github.com/flamarion/pfv/actions/workflows/test.yml` |
| TFC `<data-workspace>` (DO data droplet) | `https://app.terraform.io/app/<tfc-org>/workspaces/<data-workspace>` |
| TFC `<apex-workspace>` (AWS apex) | `https://app.terraform.io/app/<tfc-org>/workspaces/<apex-workspace>` |
| DO App Platform deploys | DO console -> Apps -> `pfv` -> Activity |
| Backend access logs (live) | DO console -> Apps -> `pfv` -> Runtime Logs -> backend component |
| Frontend access logs (live) | DO console -> Apps -> `pfv` -> Runtime Logs -> frontend component |
| `PRE_DEPLOY migrate` job logs | DO console -> Apps -> `pfv` -> Activity -> select deploy -> migrate job |
| Apex CloudFront access logs | Not enabled today. `infra/terraform/apex/main.tf` (`aws_cloudfront_distribution.apex`) does not configure `logging_config`. Post-launch follow-up: provision a separate S3 bucket for CloudFront standard logs and add the logging block. For real-time debugging until then, AWS console -> CloudFront -> distribution -> Monitoring tab. |
| Apex S3 contents | AWS console -> S3 -> `thebetterdecision-com-apex` |
| MySQL slow query / error log | SSH to `<data-droplet>`: `journalctl -u mysql` or `/var/log/mysql/error.log` |
| Nightly mysqldump | `<data-droplet>`: `ls -lh /var/backups/mysql/`; log at `/var/log/mysql-backup.log` |
| Smoke-test failure GitHub issue | Auto-opened by `scripts/notify-smoke-failure.sh`; check open issues in `flamarion/pfv` |

Triage shortcuts:

| Symptom | First look at |
|---|---|
| Merge to `main` happened, prod didn't update | `release.yml` -> did `release` job set `new_release_published=true`? Conventional commit type may be `chore` |
| Deploy went green, app still broken | Smoke-test job output, then DO Runtime Logs on the failing component |
| Migrate job hung or failed | DO Activity -> latest deploy -> migrate job. Grep for `migrate.start`, `migrate.failed`, `migrate.step.start`. Multi-head? Driver error? |
| Apex site shows stale content | Confirm `apex-deploy.yml` ran for the SHA; check CloudFront invalidation completed; `curl https://thebetterdecision.com/_meta.json` (object is no-cache). If the apex hostname is itself unreachable, fall back to the TFC output `cloudfront_distribution_domain` to probe the distribution directly. |
| Apex 404 on a known route | The CloudFront Function rewrites `/path/` -> `/path/index.html`. Check the function's invocation logs in CloudFront Functions console |
| Apex deploy failed at OIDC step | Trust policy on `github-actions-apex-deploy` pinned to `repo:flamarion/pfv:ref:refs/heads/main`. PR-context, forks, non-main branches are rejected by design |
| App can't reach MySQL or Redis | Confirm `.do/app.yaml`'s top-level `vpc.id` matches the TFC output, and `DATABASE_URL` / `REDIS_URL` point at the droplet's `<vpc-cidr>` private IP |
| Secret env var "disappeared" after deploy | `.do/app.yaml` must declare every SECRET with its `EV[...]` blob. Missing -> stripped on push. Refresh via `doctl apps spec get <app-id>` |

For the env var matrix and common per-variable failures (Google SSO button missing, `NEXT_PUBLIC_*` not in client bundle, audit log shows ingress IP, etc.), see [`ENVIRONMENT.md`](./ENVIRONMENT.md) "Common failure modes".
