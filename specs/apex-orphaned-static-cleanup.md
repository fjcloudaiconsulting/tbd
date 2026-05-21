---
name: Apex S3 — clean up orphaned _next/static chunks
description: PR #267 stopped using --delete on the immutable hashed-asset sync to avoid 404ing browser-cached HTML, leaving orphaned chunks accumulating. Needs a periodic cleanup.
type: project
---
# Orphaned hashed-asset cleanup for apex S3

## Background

PR #267 (apex deploy workflow) changed the sync strategy after owner review caught a race:
- Old behavior: `aws s3 sync out-apex/_next/static/ s3://bucket/_next/static/ --delete` could delete chunks still referenced by browser-cached HTML (5-min TTL), producing 404s for users mid-deploy.
- New behavior: hashed-asset sync uploads first WITHOUT --delete. Mutable sync runs second WITH --delete and `--exclude "_next/static/*"`. Safe deploy order; chunks always exist before HTML references them; old chunks survive the deploy window.

Trade-off: hashed chunks orphaned by a rename (Next.js content-hashes filenames on every change) are never pruned by the deploy. They accumulate forever.

## What PR #240's lifecycle rule does NOT cover

The S3 bucket has `aws_s3_bucket_lifecycle_configuration.apex` expiring noncurrent versions after 90 days. That rule handles objects where the SAME KEY gets overwritten and the old version becomes noncurrent. It does NOT handle hashed chunks whose key changes entirely (e.g., `app-abc123.js` → `app-def456.js` is a new key, the old one remains current forever from S3's POV).

## Why it's low-priority

- Each hashed chunk is small (KB to low MB). Even with daily deploys, annual storage growth is at most a few hundred MB.
- S3 Standard is ~$0.023/GB/mo. Years of accumulation costs cents.
- No correctness or performance issue, purely housekeeping.

## Two implementation options

### Option A: S3 lifecycle rule (IaC, preferred)

Add a lifecycle rule to `infra/terraform/apex/main.tf`'s `aws_s3_bucket_lifecycle_configuration.apex`:

```hcl
rule {
  id     = "expire-orphaned-next-static"
  status = "Enabled"
  filter {
    prefix = "_next/static/"
  }
  expiration {
    days = 30
  }
}
```

**Risk:** this expires the CURRENT version after 30 days, NOT just orphans. Any chunk that was uploaded 31 days ago and is still referenced by current HTML would 404. The deploy workflow re-uploads chunks on every deploy so any in-use chunk gets its mtime refreshed, but only if it's part of the new build output. A chunk that hasn't changed across deploys (rare for Next.js but possible for content-stable utility chunks) could age out while still in use.

Mitigation: bump to 90 days, or run apex deploy on a regular cadence (cron) so all current chunks stay refreshed.

### Option B: Scheduled cleanup workflow (GH Actions, more precise)

Add a weekly GH Actions workflow that:
1. Fetches the current bucket's `_next/static/` object list.
2. Downloads the current `index.html` and crawls referenced `/_next/static/...` URLs (or downloads `_meta.json` to confirm which build SHA is current).
3. Computes the diff: objects in bucket not referenced by current HTML.
4. Deletes orphans older than N days.

More precise but more code, more failure modes. Probably overkill for a landing site.

## Recommended path

Option A with `days = 90`, scoped to `_next/static/` prefix. The 90-day window matches the existing noncurrent-version expiry, gives a generous safety margin, and is IaC-managed (no scheduled job to maintain).

Effort: XS. One Terraform PR, single resource block addition.

## When to do this

Post-launch. Storage cost is irrelevant for the first ~6 months even if we deploy 10x/day.
