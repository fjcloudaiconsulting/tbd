---
name: L5.2a Apex S3+CloudFront — D1-D5 decisions summary (post-ship audit)
description: Decision-elicitation pass on the L5.2a Apex spec. Discovered the implementation already shipped (PRs #240, #241, #267, #270, #271, #266, #268). This memo reconciles the spec's "open" D1-D5 with the locked answers that landed in code, and surfaces what is genuinely still open.
type: project
---

# L5.2a Apex S3+CloudFront — D1-D5 decisions summary

## Why this matters

The L5.2a spec at `specs/apex-s3-cloudfront-l5-2a.md` still labels D1-D5 as "open owner decisions" pending lock. In reality the dispatch sequence fired between PR #240 and PR #271, so every decision now has a concrete answer in merged code. This memo reconciles the spec's framing with shipped reality, so the architect can confirm the locked answers, retire the "decisions open" framing, and direct attention to what remains.

## Current state

`thebetterdecision.com` and `www.thebetterdecision.com` are served from AWS S3 + CloudFront in `eu-central-1` (Frankfurt), with ACM cert in `us-east-1` (CloudFront constraint), OAC-only access to the bucket, security headers via a response-headers policy, and a CloudFront Function handling `www` to apex 301 + S3 directory-index rewrites. `app.thebetterdecision.com` continues to serve from DigitalOcean App Platform (no Cloudflare CDN, no WAF in front of it). Cloudflare is in the trust boundary only via Turnstile CAPTCHA on `POST /api/v1/auth/register` (script + siteverify), not as DNS, CDN, or WAF for any host. Terraform Cloud workspace `FlamaCorp/pfv-apex` owns the AWS resources via OIDC workload identity; GitHub Actions deploys via OIDC into a least-privilege role pinned to `repo:flamarion/pfv:ref:refs/heads/main`. No long-lived AWS keys remain after bootstrap. Production data plane (MySQL, Redis) stays on the self-hosted `pfv-data-01` droplet in a private VPC, unchanged.

## Decisions table

| ID | Name | Options (spec) | Locked answer | Where it landed |
|---|---|---|---|---|
| D1 | Cloudflare scope | A: apex only / B: entire stack | **D1.A** — apex only | `reference_cloudflare_trust_boundary.md` (Turnstile retained on /register); apex on AWS, app. on DO direct |
| D2 | AWS account | A: existing personal / B: new AWS Org / C: middle ground | **D2.A** — existing personal account, `aws_account_id` set explicitly in TFC | `infra/terraform/apex/variables.tf:1` (required, no default) |
| D3 | Build pipeline shape | A: single repo two outputs / B: extract apex/ / C: hand-rolled HTML | **D3.A** — `build:apex` script + path filter on existing `frontend/` | PR #241, `frontend/scripts/build-apex.sh`, `.github/workflows/apex-deploy.yml` |
| D4 | Credential mechanism | A: static IAM keys / B: OIDC federation | **D4.B** — OIDC for both TFC and GH Actions; bootstrap user deleted post-first-apply | `infra/terraform/apex/main.tf` (`aws_iam_openid_connect_provider` x2), `apex-deploy.yml` |
| D5 | Cutover phasing | 5-phase plan, only phase 5 irreversible | **Adopted as written** — PR-A, B, C, D in order; plus IAM propagation grace (#271) | PRs #240, #267, #241, #270, #271 |

## D1-D5 detail

### D1 — Cloudflare scope

Decision: Cloudflare is removed from the apex landing path entirely (no DNS, CDN, or WAF in front of `thebetterdecision.com`).

Locked answer: **D1.A**. Cloudflare is still part of the trust boundary on `app.` for one narrow path: Turnstile CAPTCHA on `/register` (widget script from `challenges.cloudflare.com` + server-side siteverify). That is API-call only, not DNS or proxying. `app.thebetterdecision.com` resolves directly to DigitalOcean App Platform ingress with no intermediate CDN.

Trade-offs reconciled:
- Loss vs D1.B: app. has no edge WAF in front. App Platform ingress is the public entry; rate limiting and bot defense live in FastAPI middleware + Turnstile.
- Gain vs D1.B: lower vendor exposure, simpler trust boundary, fewer DNS hops, no Cloudflare-outage blast radius on the app surface.

Recommendation: **confirm D1.A as final.** A future decision on putting Cloudflare (or an alternative) in front of `app.` for WAF and DDoS should be tracked as a separate L-band item, not folded back into L5.2a.

Downstream impact: shipped. The apex Terraform module assumes no Cloudflare layer.

Confidence: **high.**

### D2 — AWS account

Decision: which AWS account owns the apex resources (bucket, distribution, cert, IAM roles).

Locked answer: **D2.A**. The owner's existing personal AWS account holds everything. `aws_account_id` in `variables.tf` has no default and must be set explicitly in the TFC workspace; the constraint is enforced via a regex validator and via least-privilege IAM ARNs that bake the account ID in.

Trade-offs reconciled:
- Vs D2.B (dedicated AWS Org account): D2.B would give a cleaner blast-radius boundary if AWS grows beyond landing. Today AWS surface is one bucket and one distribution, so the boundary benefit is theoretical.
- Vs D2.C (mixed budget umbrella): irrelevant once D2.A landed.

Recommendation: **confirm D2.A as final for the v1 footprint.** If AWS surface ever expands to multi-region, multi-app, or staging environments, revisit D2.B at that point.

Downstream impact: shipped. The TFC workspace `FlamaCorp/pfv-apex` was created against the chosen account.

Confidence: **high.**

### D3 — Build pipeline shape

Decision: how to produce the static export for the apex landing.

Locked answer: **D3.A**. The existing `frontend/` Next.js project gained a second build target via `npm run build:apex` (script in `frontend/scripts/build-apex.sh`, configured to emit `out-apex/`). Landing components, brand tokens, and the `LandingAuthRedirect` island are shared with `app.`. The GitHub Actions workflow path-filters on landing surface paths and deploys only `out-apex/` to S3.

Trade-offs reconciled:
- Vs D3.B (extract apex/): would have required a workspace package or component duplication, and a second CI lane. Avoided.
- Vs D3.C (hand-rolled HTML): would have decayed quickly once any landing iteration shipped. Avoided.

Recommendation: **confirm D3.A as final.** The `LandingAuthRedirect` JS-on-apex concern from the spec is moot in practice (negligible byte cost; not blocking).

Downstream impact: shipped. PR #279 demonstrated this is iteration-friendly (added `HowItWorks` component without touching the build pipeline).

Confidence: **high.**

### D4 — Credential mechanism

Decision: how Terraform Cloud and GitHub Actions authenticate to AWS.

Locked answer: **D4.B**. OIDC federation for both. The bootstrap path used a single static-key admin IAM user (`pfv-apex-bootstrap`) for the very first TFC apply, then flipped TFC to OIDC via `TFC_AWS_PROVIDER_AUTH=true` + `TFC_AWS_RUN_ROLE_ARN`. The bootstrap user is documented as needing deletion within an hour. GitHub Actions trusts a `repo:flamarion/pfv:ref:refs/heads/main`-pinned sub claim. The IAM role for deploys can only `PutObject`/`DeleteObject`/`ListBucket` on the apex bucket and `CreateInvalidation` on the apex distribution.

Trade-offs reconciled:
- Vs D4.A (static keys): D4.A would have meant two long-lived IAM users with manually rotated keys stored in TFC env vars and GH secrets. D4.B eliminates that surface entirely.
- The OIDC thumbprints are computed at plan time via `data "tls_certificate"` so they survive issuer-cert rotations without manual intervention.

Recommendation: **confirm D4.B as final.** Verify with the architect that the bootstrap IAM user was actually deleted (the README documents the step but doesn't enforce it).

Downstream impact: shipped. The PR #268 fix (`route53:ListTagsForResource{,s}`) and PR #271 (IAM propagation grace via `time_sleep`) are direct followups to operating the OIDC trust under TFC's apply timing.

Confidence: **high** on the design; **medium** on the bootstrap-cleanup hygiene (needs owner confirmation that the static-key IAM user is actually gone).

### D5 — Cutover phasing

Decision: how to roll the DNS over from "apex parked" to "apex on CloudFront" without an irreversible blast radius.

Locked answer: **adopted as written**. PR-A (#240) provisioned the AWS infrastructure with no DNS changes. PR-B (#267) added the deploy workflow. PR-C (#241) added the static-export build target. PR-D (#270) flipped the apex `A`/`AAAA` ALIAS to the CloudFront distribution. The unplanned PR #271 added an IAM-propagation `time_sleep` gate after the OIDC role was created; without it, the first apply could race the AWS IAM consistency window. PR #266 set `aws_region` default to Frankfurt (`eu-central-1`) for EU-resident user-base proximity (CloudFront is global; this only affects the S3 origin).

Trade-offs reconciled:
- Each phase was its own small Terraform PR. Rollback at any phase before phase 4 was free. Phase 5 rollback path was `git revert PR-D` with 60s TTL on the apex record.
- Two operational followups landed mid-flight (#271, #268) which the original phasing did not call out. Both were "obvious in retrospect" gotchas (IAM consistency, route53 read perms for TFC's read step). They are now known patterns for any future similar work.

Recommendation: **confirm D5 as final and shipped.** Capture the two mid-flight findings as reference notes for the next AWS+OIDC bootstrap (currently implicit in the README; could be promoted into the gotchas memory section).

Downstream impact: shipped. The dispatch sequence ran to completion; apex is live.

Confidence: **high.**

## What is genuinely still open

These are NOT D1-D5, but they sit adjacent to L5.2a and the architect may want a decision on each:

1. **Orphaned hashed-asset cleanup.** `specs/apex-orphaned-static-cleanup.md` describes an S3-lifecycle rule (90 days, scoped to `_next/static/`) vs a scheduled GH Actions crawl. Recommended path: Option A with `days = 90`. Effort: XS. Status: deferred to post-launch per the spec, low priority.
2. **Cloudflare (or alternative) in front of `app.`.** D1.A explicitly left this open. Today `app.` has no edge WAF or DDoS layer; FastAPI middleware + Turnstile are the only defenses. A separate spec should pick this up if WAF coverage becomes a launch requirement.
3. **L5.1 full-scope landing items.** Pricing preview, FAQ, testimonials, animations, product screenshots all remain unbuilt. PR #279 shipped the "How it works" iteration. These are L5.1 work, not L5.2a, but they live on the surface L5.2a delivers.
4. **PR preview deploys.** The apex README explicitly calls out that the current IAM trust policy makes per-PR preview deploys impossible by design. If preview-on-PR ever becomes a requirement, a separate read-only role with a different sub-claim condition is needed.

## Dispatch sequence (historical)

The original dispatch sequence ran in this order, all shipped:

1. **PR-A — #240** Terraform: S3, CloudFront, ACM, OIDC providers, IAM roles, Route 53 ACM validation records only. No apex A record change.
2. **PR-B — #267** GitHub Actions deploy workflow with OIDC role assumption, path filters mirroring the DO release workflow's negation list.
3. **PR-C — #241** Next.js static-export build target for the landing surface (`build:apex` script, `out-apex/` output).
4. **PR-D — #270** Terraform: apex/www `A`+`AAAA` ALIAS records flipped to the CloudFront distribution. TTL drop preceded the swap.
5. **Mid-flight followups:** #271 (IAM propagation grace), #268 (TFC route53 read perms), #266 (eu-central-1 default).

For any FUTURE similar AWS+OIDC bootstrap, the dispatch sequence template is the same with two pre-known additions: (a) include the IAM `time_sleep` grace from day one, and (b) include the route53 read perms in the TFC role from day one.

## Touch points (already landed)

- `infra/terraform/apex/main.tf` (880 LOC, S3 + CloudFront + ACM + IAM OIDC + IAM roles + Route 53)
- `infra/terraform/apex/variables.tf`, `outputs.tf`, `versions.tf`, `providers.tf`, `terraform.tfvars.example`
- `infra/terraform/apex/README.md` (bootstrap runbook + security notes + rollback)
- `.github/workflows/apex-deploy.yml`
- `frontend/scripts/build-apex.sh` and the `build:apex` package.json script
- `frontend/next.config.*` (apex-export aware)

## Architect-review notes (self-review pass)

The original task asked for a `voltagent-qa-sec:architect-reviewer` subagent pass; that tool was not available in this session, so the review was done inline against the same checklist. Items flagged for owner confirmation:

- **Bootstrap IAM user lifecycle.** `pfv-apex-bootstrap` (Administrator-scoped) was created for the very first TFC apply. The README documents deleting it "within an hour" but the step is operator-driven, not enforced. Confirm it is gone (or has its access key deactivated). If still present, it is the single highest-privilege artifact in the AWS account and should be the next thing closed.
- **No edge WAF in front of `app.`** D1.A made this an explicit non-decision for v1. Worth re-confirming this is acceptable for launch, given that Turnstile only protects `/register`. Login, password reset, refresh, and every authenticated endpoint sit behind App Platform ingress with FastAPI middleware as the only defense. A future spec to put a WAF (Cloudflare, AWS WAF via a new CloudFront in front of App Platform's domain, or DO's own offering once GA) in front of `app.` is worth tracking.
- **CloudFront cost ceiling.** No spend cap is documented. A traffic spike (legitimate or otherwise) on a `$0/month` CloudFront distribution can scale to surprise numbers. Consider an AWS Budgets alert at $25/month or similar as a guard rail.
- **Region split is intentional.** S3 + Lambda@Edge originals live in `eu-central-1`; ACM cert lives in `us-east-1` (CloudFront constraint). Documented and correct.
- **GDPR/landing analytics.** Landing surface today serves static HTML with no analytics or cookies. If analytics get added (Plausible, GA4, Vercel Analytics, etc.) revisit the L1.4 cookie audit before merge.

## Recommended next steps for the architect

1. Read the "Decisions table" above. If every locked answer is acceptable, comment "confirm D1-D5 as locked, retire as-open framing in `specs/apex-s3-cloudfront-l5-2a.md`."
2. If any D needs revisiting, name it and the alternative.
3. Decide whether the four "genuinely still open" items become individual L-band tickets or stay informal.
4. Confirm the bootstrap IAM user (`pfv-apex-bootstrap`) was actually deleted post-bootstrap, or schedule its deletion if not.
5. Decide whether an AWS Budgets alert (e.g., $25/month) should be added as a cost guard rail.
