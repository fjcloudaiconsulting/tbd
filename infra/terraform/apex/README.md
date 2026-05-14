# pfv terraform: apex landing (AWS S3 + CloudFront + ACM + IAM OIDC)

Terraform Cloud workspace `FlamaCorp/pfv-apex` managing the AWS-side
infrastructure for the `thebetterdecision.com` apex marketing site.
`app.thebetterdecision.com` stays on DigitalOcean App Platform. State and
runs live in TFC; this directory holds the configuration.

This module is the AWS side of the L5.2a apex split. It manages the S3
bucket, CloudFront distribution, ACM cert in `us-east-1`, the two IAM
OIDC providers (GitHub Actions + TFC), the apex-deploy and provisioner
IAM roles, and the Route 53 records that point the apex hostnames at
the distribution (`A` and `AAAA` ALIAS records for the apex and `www`,
plus the ACM-emitted validation CNAMEs).

## Resources managed

| Resource | Purpose |
|---|---|
| `aws_s3_bucket` (+ public-access-block, versioning, SSE, lifecycle, ownership) | Private origin bucket for the static export |
| `aws_cloudfront_distribution` | Edge distribution with HTTPS, HSTS, www -> apex redirect |
| `aws_cloudfront_origin_access_control` | OAC (NOT legacy OAI) so only this distribution can read the bucket |
| `aws_cloudfront_function` | Viewer-request function: (1) `www` -> apex 301 redirect, then (2) S3 directory-index rewrite (`/privacy/` -> `/privacy/index.html`) |
| `aws_cloudfront_response_headers_policy` | Security headers: HSTS, X-Frame-Options, Referrer-Policy, Permissions-Policy |
| `aws_acm_certificate` + `_validation` | DNS-validated cert in `us-east-1` (CloudFront requirement) for apex + www |
| `aws_route53_record.apex_acm_validation` | ACM `_<token>.<domain>` validation CNAMEs in the existing zone. **Does NOT touch the apex A record.** |
| `aws_iam_openid_connect_provider.github` | Trust for GitHub Actions OIDC tokens |
| `aws_iam_openid_connect_provider.tfc` | Trust for Terraform Cloud workload identity tokens |
| `aws_iam_role.github_actions_apex_deploy` | Deploy role (s3 put/delete + cloudfront invalidation, scoped to this bucket + distribution) |
| `aws_iam_role.tfc_apex_provisioner` | TFC-assumed role for managing this module's resources |

## Workspace variables

Set in TFC (`FlamaCorp/pfv-apex` -> Variables); never committed.

| Name | Kind | Sensitive | Description |
|---|---|---|---|
| `aws_account_id` | Terraform | optional | 12-digit AWS account ID owning the apex resources. Strictly not a secret, but commonly marked Sensitive in TFC so it does not appear in plan/apply output. |
| `AWS_ACCESS_KEY_ID` | Env | yes | **Bootstrap only**. Delete after first apply (see Bootstrap). |
| `AWS_SECRET_ACCESS_KEY` | Env | yes | **Bootstrap only**. Delete after first apply (see Bootstrap). |
| `TFC_AWS_PROVIDER_AUTH` | Env | no | Set to `true` after bootstrap to switch TFC to OIDC. |
| `TFC_AWS_RUN_ROLE_ARN` | Env | no | After bootstrap, the `tfc_role_arn` output. Tells TFC to assume that role via workload identity. |

Defaults for `aws_region`, `domain`, `github_repo`, `tfc_organization`,
`tfc_workspace_pattern`, and `noncurrent_version_expiration_days` live in
`variables.tf` and rarely need overriding.

## Outputs (consumed by other PRs)

Read from TFC -> Workspace -> Outputs after apply.

- `github_actions_role_arn` -> the `apex-deploy.yml` workflow consumes
  this as `role-to-assume` in `aws-actions/configure-aws-credentials`.
  Surfaced to GitHub as the repo variable `AWS_APEX_DEPLOY_ROLE_ARN`.
- `s3_bucket_name` -> consumed by the deploy workflow for
  `aws s3 sync out-apex/ s3://<bucket>/`. GitHub variable
  `AWS_APEX_BUCKET`.
- `cloudfront_distribution_id` -> consumed by the deploy workflow for
  `aws cloudfront create-invalidation`. GitHub variable
  `AWS_APEX_DISTRIBUTION_ID`. Also the alias target referenced by the
  apex/www ALIAS records.
- `cloudfront_distribution_domain` -> the `dXXX.cloudfront.net`
  hostname. Useful for diagnostics (pinging the distribution directly,
  bypassing DNS) and as a fallback verification path during incidents.
- `cloudfront_distribution_hosted_zone_id` -> referenced internally by
  the apex/www ALIAS records (`alias.zone_id`). Stable AWS constant
  (`Z2FDTNDATAQYW2`), exposed as an output for visibility.
- `route53_zone_id` -> the apex hosted zone ID. Used by the validation
  CNAMEs and the apex/www ALIAS records in this module.
- `tfc_role_arn` -> set as `TFC_AWS_RUN_ROLE_ARN` env variable in the
  `pfv-apex` workspace after the bootstrap apply. Lets TFC drop static
  keys.

## Workflow

- **Speculative plan**: every PR touching `infra/terraform/apex/**` gets
  a TFC plan posted as a status check on the PR.
- **Apply**: triggered on merge to `main`, gated on **manual confirm** in
  the TFC UI. Auto-apply is intentionally off, matching the
  `FlamaCorp/pfv` (data droplet) workspace policy. No infra change ever
  lands without an operator clicking Confirm & Apply.
- **Local CLI**: `terraform login` once, then
  `terraform -chdir=infra/terraform/apex plan` reaches the same remote
  state. CLI plan/apply is debug-only per the workspace policy
  (`feedback_terraform_vcs_only`).

## Bootstrap

First TFC apply needs **something** to authenticate to AWS, because the
OIDC providers and the `tfc-apex-provisioner` role only exist after this
module applies. We use a **single static-credential bootstrap run**, then
flip to OIDC immediately.

### One-time bootstrap (owner runs)

1. In AWS, create an IAM user named `pfv-apex-bootstrap` with
   `AdministratorAccess`. Generate an access key pair.
2. In TFC -> `FlamaCorp/pfv-apex` -> Variables, add:
   - `AWS_ACCESS_KEY_ID` (Environment, sensitive) = the key id
   - `AWS_SECRET_ACCESS_KEY` (Environment, sensitive) = the secret
   - `aws_account_id` (Terraform) = your 12-digit account id
3. Configure the workspace's VCS settings:
   - Repo: `flamarion/pfv`
   - Working directory: `infra/terraform/apex`
   - Trigger pattern: `infra/terraform/apex/**`
   - Auto-apply: **off**
4. Open this PR. TFC runs a speculative plan against the worktree;
   inspect the plan carefully, then merge.
5. On merge, TFC creates an apply run. Click **Confirm & Apply** in the
   TFC UI. The run provisions S3, CloudFront, ACM (validates via DNS,
   takes 2-5 minutes), the OIDC providers, and the two IAM roles.
6. **Switch TFC off static keys**:
   - In TFC -> `FlamaCorp/pfv-apex` -> Variables, set:
     - `TFC_AWS_PROVIDER_AUTH` (Environment) = `true`
     - `TFC_AWS_RUN_ROLE_ARN` (Environment) = the `tfc_role_arn` output
       from the bootstrap apply (e.g.
       `arn:aws:iam::123456789012:role/tfc-apex-provisioner`)
   - Delete `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` from the
     workspace variables.
7. In AWS, delete the `pfv-apex-bootstrap` IAM user (or at minimum
   deactivate its access key). Do this within an hour of the bootstrap
   apply.
8. Trigger a no-op plan in TFC to confirm OIDC works end-to-end. Empty
   plan = success.

### Why this path (B) over manual OIDC bootstrap (A)

We considered a path where the owner manually creates the OIDC providers
in the AWS console before the first TFC run. That avoids ever using
static keys, but it splits the resource graph: the OIDC providers exist
outside Terraform state, so subsequent applies that touch their
thumbprints or trust policies have to be done manually too. Path B keeps
everything in Terraform from day one, at the cost of one hour of an
admin-scoped IAM user. The static-keys window is tightly bounded.

## Module layout

```
.
├── main.tf                   # S3, CloudFront, ACM, IAM OIDC + roles
├── variables.tf              # All inputs + defaults
├── outputs.tf                # Consumed by PR-B and PR-D
├── versions.tf               # cloud{} (TFC), provider pins
├── providers.tf              # Default + us-east-1 alias for ACM
├── terraform.tfvars.example  # Reference only; TFC sets vars in the workspace
├── .gitignore
└── README.md                 # This file
```

This module is intentionally **flat** (single directory, no submodules)
because each AWS resource here is referenced by exactly one consumer.
The `infra/terraform/` (DO data droplet) module nests `vpc/`,
`droplet/`, and `firewall/` because each is reused across attachment
targets; apex has no such reuse story.

## Security notes

### Least privilege

- The **GitHub Actions deploy role** can `PutObject`, `DeleteObject`,
  `ListBucket` on the apex bucket only, and `CreateInvalidation` on the
  apex distribution only. No other bucket, no other distribution. The
  trust policy uses `StringEquals` on the OIDC `sub` claim, pinned to
  exactly `repo:flamarion/pfv:ref:refs/heads/main`. PR-context tokens
  have a different `sub` and are rejected at the trust level. Workflow
  `if:` guards alone would be insufficient because PR authors can edit
  the workflow file; this restriction is IAM-enforced.
  - **PR previews are not possible with this role.** If a future
    requirement asks for preview deploys per PR, the correct path is a
    separate read-only IAM role with its own trust policy (e.g.
    conditioned on `pull_request` sub patterns and granted only
    `s3:ListBucket`/`s3:GetObject`). That role is deliberately out of
    scope for PR-A.
- The **TFC apex provisioner role** can manage the apex bucket and
  distribution, ACM certs in `us-east-1`, the two IAM OIDC providers,
  and its own + the GH Actions role. Route 53 writes are split into two
  narrowly-scoped IAM statements, each pairing
  `route53:ChangeResourceRecordSetsRecordTypes` with
  `route53:ChangeResourceRecordSetsNormalizedRecordNames`:
    - `A` and `AAAA` ALIAS writes are allowed ONLY on
      `thebetterdecision.com` and `www.thebetterdecision.com` (the
      apex / www CloudFront ALIASes).
    - `CNAME` writes are allowed ONLY on names matching
      `_*.thebetterdecision.com` or `_*.www.thebetterdecision.com` (the
      ACM validation pattern).
  Any other record type or any other name in the zone is **IAM-blocked**,
  so a leaked role token cannot rewrite MX records, NS delegations,
  arbitrary subdomain A records, or any other zone content.
- The **S3 bucket** has all four public-access-block flags ON. Read
  access is granted exclusively to the apex CloudFront distribution
  service-principal, scoped by `SourceArn` condition.

### OIDC thumbprint rotation

Both `aws_iam_openid_connect_provider` resources compute their SHA-1
thumbprints at plan time via `data "tls_certificate"` lookups against
the live OIDC issuer endpoints
(`token.actions.githubusercontent.com` and `app.terraform.io`). AWS does
NOT auto-rotate OIDC thumbprints, so hand-pinning is fragile across
issuer cert rotations; computing on apply keeps trust intact. Requires
the `hashicorp/tls ~> 4.0` provider, declared in `versions.tf`.

### S3 directory-index handling (CloudFront Function)

S3 REST origin (the OAC path) does NOT translate `/privacy/` to
`/privacy/index.html`. That's only the S3 static-website-hosting
endpoint, which is public + non-HTTPS and we deliberately avoid. A
viewer-request CloudFront Function rewrites directory-style URIs:

- `/privacy/` -> `/privacy/index.html`
- `/about` (no trailing slash, no extension) -> `/about/index.html`
- `/_next/static/foo.css` (has extension) -> pass through

The function ALSO performs the `www` -> apex 301 redirect. Order
matters: redirect runs FIRST (so we never spend rewrite cycles on
requests we're about to bounce to a different host), rewrite runs
SECOND (so requests that survive the redirect have S3-resolvable URIs).

Expected behaviour, verifiable against the CloudFront-assigned
`dXXX.cloudfront.net` URL once PR-B has synced PR-C's `out-apex/`:

| URL | Expected response |
|---|---|
| `https://<cf-domain>/` | `index.html` (200) |
| `https://<cf-domain>/privacy/` | `privacy/index.html` (200) |
| `https://<cf-domain>/privacy` | `privacy/index.html` (200 after rewrite) |
| `https://<cf-domain>/terms/` | `terms/index.html` (200) |
| `https://<cf-domain>/docs/` | `docs/index.html` (200) |
| `https://<cf-domain>/_next/static/foo.css` | 200 (no rewrite) |
| `https://www.<apex>/privacy` | 301 -> `https://<apex>/privacy` |

### OAC vs OAI

We use Origin Access Control (OAC, the AWS-recommended successor to OAI)
because OAC supports SigV4 to the bucket, dynamic-content origins, and
KMS-encrypted buckets. Legacy OAI is in maintenance mode and AWS has
documented OAC as the long-term path.

### Headers

- `Strict-Transport-Security: max-age=63072000; includeSubDomains; preload`
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy: accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()`

CSP is intentionally **not** set in this module. PR-C will author the
CSP alongside the static-export's actual asset graph and add it via a
follow-up to the response-headers policy.

## Rollback

Every resource in this module is `terraform destroy`-able. A full
teardown removes the apex / www ALIAS records, the ACM validation
CNAMEs, the bucket and distribution, the IAM roles and OIDC providers.
The apex hosted zone itself is data-sourced, not managed, so it
survives untouched. After teardown DNS for the apex and www returns
to "no answer", and rerunning a fresh apply recreates everything end
to end.

For a partial rollback that only undoes the DNS cutover (keeping the
bucket / distribution / cert / roles intact so the apex-deploy
pipeline keeps working against `cloudfront_distribution_domain`),
revert PR-D via `git revert` and let TFC apply the inverse. The
break-glass CLI path is documented in PR-D's PR body.

## Cost

| Line | Monthly |
|---|---|
| S3 storage (static export, ~5 MB) | <$0.01 |
| S3 requests (deploys + invalidations) | <$0.10 |
| CloudFront (PriceClass_100, modest traffic) | ~$1.00 |
| ACM certificate | $0.00 (free for CloudFront) |
| Route 53 zone (existing) | $0.50 (not new) |
| IAM OIDC providers | $0.00 |
| **Total new spend** | **~$1.10** |

## See also

- `../README.md`: DO data droplet workspace (`FlamaCorp/pfv`)
- `~/.claude/projects/-Users-fjorge-src-pfv/memory/project_apex_s3_cloudfront.md`:
  canonical plan and locked decisions (D1-D5)
- `~/.claude/projects/-Users-fjorge-src-pfv/memory/feedback_terraform_vcs_only.md`:
  Terraform is VCS-driven; CLI is debug-only
