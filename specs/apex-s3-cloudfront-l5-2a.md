---
name: Apex landing on AWS S3 + CloudFront (L5.2a)
description: Strategic shift 2026-05-12 — drop Cloudflare from the apex landing plan; host the apex marketing site on AWS S3 (private) + CloudFront (OAC + ACM) instead. Standard SaaS pattern. Brainstorm + open decisions captured here pending owner decisions.
type: project
---
# Apex landing on AWS S3 + CloudFront

**Locked direction (2026-05-12):** apex `thebetterdecision.com` will be served from AWS S3 + CloudFront. `app.thebetterdecision.com` stays on DigitalOcean App Platform. Cloudflare is dropped from the apex plan.

**Status:** brainstorm, open decisions surfaced below. No dispatch until decisions are locked.

## Recommended architecture (one canonical stack)

```
Route 53 zone (thebetterdecision.com, existing)
├── thebetterdecision.com           A     → ALIAS → CloudFront distribution
├── www.thebetterdecision.com       A     → ALIAS → CloudFront distribution (same)
└── app.thebetterdecision.com       CNAME → ondigitalocean.app (unchanged)

CloudFront distribution
├── Default origin: S3 bucket (private, OAC, region eu-west-1 or us-east-1)
├── TLS: ACM cert in us-east-1 (CloudFront requirement) for apex + www
├── Response-headers policy: HSTS, X-Content-Type-Options, X-Frame-Options, Referrer-Policy
├── Cache policy: long TTL on hashed assets, short TTL on /index.html
└── Behaviors: redirect HTTP -> HTTPS, optionally www -> apex via Lambda@Edge or CloudFront Function

S3 bucket
├── Block all public access (bucket private, OAC is the only reader)
├── Versioning ON (rollback)
└── Server-side encryption (SSE-S3)

IAM
├── GitHub Actions OIDC role: s3:PutObject + s3:DeleteObject + cloudfront:CreateInvalidation
└── TFC OIDC role: full infra provisioning (s3, cloudfront, route53, acm, iam)

Build + deploy
├── GitHub Actions on push to main touching landing/* or matching path filter
├── Builds Next.js static export of the landing surface only
├── Aws s3 sync to bucket
└── CloudFront invalidation for /index.html and /
```

## Open owner decisions

### D1 — Cloudflare scope

The 2026-05-12 instruction was "remove Cloudflare from the game for now" with explicit reference to the apex landing. Two interpretations:

- **D1.A (default reading):** Cloudflare is out of the apex landing only. `app.` may still adopt Cloudflare later as a CDN/WAF if desired (currently `app.` is directly on DO App Platform).
- **D1.B (broader reading):** Cloudflare is out of the entire product stack. `app.` stays on DO without any external CDN/WAF in front of it.

**Coordinator recommendation:** D1.A. `app.` stays as-is for now; Cloudflare/CloudFront/etc. in front of `app.` is a separate later decision.

### D2 — AWS account

The roadmap and infra-followups memory don't reference an existing AWS account for this project. To proceed:

- **D2.A:** Existing personal AWS account holds the bucket + DNS. Quickest path. Owner is also account admin.
- **D2.B:** New AWS Organization with a dedicated `pfv-prod` account, owner is the org root. More boundary-clean, more setup.
- **D2.C:** Same Linode/DO budget umbrella, with AWS only used for the marketing surface. Pragmatic middle ground if a personal AWS already exists.

**Coordinator recommendation:** D2.A (existing personal AWS) for v1. Move to dedicated account if AWS surface grows past landing. Owner names which account ID + region; everything else falls out.

### D3 — Build pipeline shape

Two viable shapes for the Next.js landing static export:

- **D3.A (single repo, single Next.js app, two outputs):** Add `output: 'export'` to a SECOND build target inside the existing `frontend/` Next.js project, scoped to landing routes only. One source of truth for components; landing inherits the `<Logo />`, `lib/brand.ts`, design tokens. Single PR sequence.
- **D3.B (extract `apex/` subdirectory):** Move landing to a fresh top-level `apex/` Next.js or Astro project. Independent lifecycle, cleaner boundaries. Bigger refactor; landing components duplicate or get shared via a workspace package.
- **D3.C (hand-rolled static HTML):** Generate landing from current Next.js components into a small set of HTML files. Smallest blast radius, but the maintenance story degrades fast once we want any dynamism.

**Coordinator recommendation:** D3.A. Reuses the already-shipped landing from PR #230 (TopNav / Hero / FeatureTiles / SecondCta / LandingFooter), keeps brand assets in one place. Caveat: the existing `LandingAuthRedirect` client island calls `useAuth` — on apex it'll be a no-op (no app cookies cross domain boundaries) but it will ship as inert JS. Either keep it and accept ~5 KB inert, or stub it out at build time. Easy follow-up.

### D4 — Credential mechanism

Two real options for TFC + GH Actions ↔ AWS:

- **D4.A (static IAM access keys):** Generate two IAM users (one for TFC, one for GH Actions), store keys as TFC env variables and GH repo secrets. Quick to set up; rotation is manual.
- **D4.B (OIDC federation):** TFC and GH Actions assume IAM roles via OIDC — no long-lived secrets stored anywhere. Setup is a one-time IAM trust-policy + Terraform `aws_iam_role`. The locked policy `feedback_terraform_vcs_only` is happy here because TFC's OIDC integration runs through TFC's existing VCS-driven flow.

**Coordinator recommendation:** D4.B. No long-lived AWS credentials, audit-friendlier, matches the trajectory of the rest of the infra. Initial setup is ~30 minutes of TFC + AWS IAM console work plus a Terraform module.

### D5 — Cutover phasing

Five-phase plan, where only phase 5 is irreversible:

1. **Phase 1 — Terraform provisions (no DNS changes yet).** S3 bucket, CloudFront distribution, ACM cert (DNS-validated via Route 53, no record yet for the apex itself), IAM OIDC roles. CloudFront distribution gets a `dXXX.cloudfront.net` URL. Reversible.
2. **Phase 2 — Build + deploy pipeline.** GH Actions workflow with OIDC assume-role; pushes static export to S3; invalidates CloudFront. Verify by hitting the cloudfront.net URL directly. Reversible.
3. **Phase 3 — Soft validation.** Owner reviews the cloudfront.net URL end-to-end: layout, fonts, OG image, links to `app.thebetterdecision.com/register` + `/login`. Reversible.
4. **Phase 4 — TTL drop.** Terraform lowers Route 53 apex record TTL to 60s ahead of cutover. Reversible.
5. **Phase 5 — DNS cutover.** Terraform swaps the apex `A` ALIAS from current target to the CloudFront distribution. Rollback = revert Terraform PR. ~5-minute propagation at the dropped TTL.

**Coordinator recommendation:** adopt this phasing as-is. Each phase is its own small Terraform PR.

## Dispatch sequence (not yet fired)

Pending D1–D5 lock:

1. **PR-A — Terraform: S3 + CloudFront + ACM + OIDC roles (no DNS).** Provisions infrastructure; doesn't change Route 53 yet beyond ACM DNS validation records.
2. **PR-B — GH Actions: build + deploy workflow.** Workflow file + path filters + OIDC role ARN + invalidation step. Test against the cloudfront.net URL.
3. **PR-C — Next.js: landing static-export build target.** `next.config` second target, build script, output sanity test.
4. **PR-D — Terraform: Route 53 cutover.** Drop TTL first commit, swap apex record second commit. Owner approves apply in TFC.

Each PR is small, reversible, and dispatchable to a separate team. PR-A and PR-C can run in parallel (different files, different domains). PR-B depends on PR-A's OIDC role ARN. PR-D depends on PR-A, PR-B, PR-C all being merged.

## Estimated cost

S3: cents/month. CloudFront: ~$1/month for landing traffic. ACM: free. Route 53: $0.50/month (existing). IAM OIDC: free. Total **<$3/month** new spend.

## Risk inventory

- **DNS cutover** is the only irreversible-without-impact step. TTL drop + Terraform-managed change makes rollback fast.
- **www → apex redirect:** the spec doesn't lock this. Sane default: redirect www to apex via CloudFront Function (negligible cost; supports the canonical-URL story from L5.2).
- **OIDC trust setup error:** common failure mode. Test by running a no-op Terraform plan through TFC and a no-op `aws s3 ls` from GH Actions before any real apply.
- **ACM cert is in us-east-1 by CloudFront requirement** even if the bucket is eu-west-1. This is fine; just call it out in the Terraform module.
- **Locale / latency:** CloudFront is a global edge network; serves EU traffic from EU PoPs. No SLA concern.

## Touch points (post-decision)

- New: `infra/terraform/apex/*.tf` (S3, CloudFront, ACM, IAM OIDC, Route 53 record).
- New: `.github/workflows/deploy-apex.yml`.
- New: `frontend/next.config.apex.ts` (or equivalent flag inside the existing `next.config`).
- New: `frontend/scripts/build-apex.sh` orchestrating the static export.
- Update: `infra/terraform/README.md` to document the AWS workspace + OIDC trust.
- Update: this memo + `project_roadmap.md` once cutover lands.
