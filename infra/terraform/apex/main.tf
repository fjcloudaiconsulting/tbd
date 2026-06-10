###############################################################################
# pfv apex landing: S3 (private) + CloudFront (OAC) + ACM (us-east-1) +
# IAM OIDC roles (GitHub Actions deploy, TFC apex provisioner) +
# Route 53 ALIAS records for the apex and www hostnames.
#
# Shipped across L5.2a's four-PR sequence:
#   PR-A (#240): S3 bucket, CloudFront distribution, ACM cert, IAM OIDC
#                trust + roles, ACM DNS-validation CNAMEs. No apex A
#                record yet; the IAM-enforced invariant was CNAME-only.
#   PR-B (#267): GitHub Actions deploy workflow that builds and pushes
#                the static export into the bucket, with CloudFront
#                invalidation. Mutually exclusive path-filters in
#                release.yml so landing-only commits skip the DO redeploy.
#   PR-C (#241): Next.js apex build target (npm run build:apex) producing
#                out-apex/ from an allowlisted slice of frontend/app/.
#   PR-D (#270): Apex cutover. Adds A + AAAA ALIAS records for apex and
#                www pointing at the CloudFront distribution. Widens the
#                TFC role's Route 53 write scope to include A/AAAA on the
#                apex/www names and CNAME on the ACM validation pattern.
###############################################################################

locals {
  apex_fqdn = var.domain
  www_fqdn  = "www.${var.domain}"

  # Deterministic, unambiguous bucket name. AWS S3 bucket names are global,
  # lowercase, and DNS-safe; the apex suffix prevents collisions with any
  # future "thebetterdecision.com" bucket spun up for a different purpose.
  bucket_name = "${replace(var.domain, ".", "-")}-apex"

  # CloudFront origin id is purely a local handle within the distribution
  # config; the format matches the AWS console convention.
  s3_origin_id = "S3-${local.bucket_name}"

  # Dedicated bucket for CloudFront standard (access) logs. Kept separate
  # from the origin bucket so log writes never collide with the static
  # export surface and so the two have independent lifecycle + ownership
  # settings (the origin enforces BucketOwnerEnforced; the logs bucket must
  # allow ACLs for CloudFront's log-delivery principal — see below).
  logs_bucket_name = "${replace(var.domain, ".", "-")}-apex-logs"

  # GitHub Actions OIDC subject claim: ONLY push-to-main can assume the
  # deploy role. PR-context tokens have a different sub (`pull_request`)
  # and are rejected by the trust policy's StringEquals match. PR previews,
  # if ever needed, require a separate read-only role (documented as a
  # follow-up in apex/README.md).
  github_main_sub = "repo:${var.github_repo}:ref:refs/heads/${var.github_main_branch}"

  # TFC workload identity subject claim. The TFC docs document the run-phase
  # suffix; we accept plan + apply so PR speculative plans and merge applies
  # both work. Workspace pattern uses TFC's glob support.
  tfc_sub_pattern = "organization:${var.tfc_organization}:project:*:workspace:${var.tfc_workspace_pattern}:run_phase:*"

  # Content-Security-Policy for the apex static export. Derived directly from
  # what build-apex.sh's output actually loads (frontend/scripts/build-apex.sh
  # + frontend/app/layout.tsx + frontend/app/page.tsx). It is INTENTIONALLY
  # distinct from the Next.js app's nonce-based CSP
  # (frontend/lib/security-headers.ts): the apex is a `output: 'export'` static
  # build with NO request-time runtime, so it cannot mint per-request nonces.
  # layout.tsx's theme-bootstrap inline <script> and page.tsx's JSON-LD
  # <script type="application/ld+json"> blocks ship WITHOUT a nonce attribute on
  # the static export (readNonce() returns ""), and Next.js itself injects
  # un-nonced inline hydration bootstrap scripts. So script-src/style-src MUST
  # allow 'unsafe-inline' here (and NOT 'strict-dynamic', which would void
  # 'unsafe-inline').
  #
  # External origins the apex genuinely loads, and nothing more:
  #   * https://fonts.googleapis.com -> Google Fonts <link rel=stylesheet> (style-src)
  #   * https://fonts.gstatic.com    -> the font files that stylesheet pulls (font-src)
  # og:image (/og.png) and every other asset are same-origin ('self'). There is
  # NO analytics, CDN, Cloudflare Turnstile, or backend connect on the apex
  # surface: build-apex.sh's post-build guard hard-fails on any /api/v1
  # reference, and the auth pages that load Turnstile are staged out of the
  # apex build by the default-deny route allowlist.
  #
  # CSP violation reporting points CROSS-ORIGIN at the app's public,
  # unauthenticated report sink (https://app.${var.domain}/api/v1/security/csp-report,
  # backend/app/routers/security.py). The apex has NO same-origin backend
  # (build-apex.sh hard-fails on any /api/v1 reference), so unlike the Next.js
  # app (frontend/lib/security-headers.ts, which reports same-origin) it must
  # send reports to the app host.
  #
  # We use ONLY the legacy report-uri directive. It is fire-and-forget: the
  # browser POSTs the violation report cross-origin with no CORS preflight and
  # ignores the response, so no cross-origin allowlist on the endpoint is
  # required. report-uri is honored by Chrome, Safari, and Firefox.
  #
  # The modern Reporting API (report-to + the Reporting-Endpoints header) is
  # INTENTIONALLY omitted here. A cross-origin Reporting API endpoint IS
  # CORS-gated (the browser sends a preflight before delivering reports), so
  # enabling it would require adding the apex origin
  # (https://thebetterdecision.com) to the app backend's BACKEND_CORS_ORIGINS
  # allowlist. That is a backend trust-surface change, out of scope for this
  # infra-only landing change.
  apex_csp = join("; ", [
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "style-src-attr 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data: https://fonts.gstatic.com",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
    "upgrade-insecure-requests",
    "report-uri https://app.${var.domain}/api/v1/security/csp-report",
  ])
}

# Existing hosted zone for the apex domain. We do not create the zone here;
# it was registered earlier in the project lifecycle and lives in this same
# AWS account. Failure to find it surfaces as an explicit "no matching zone"
# error at plan time, which is the desired behaviour.
data "aws_route53_zone" "apex" {
  name         = var.domain
  private_zone = false
}

# OIDC thumbprint lookups. AWS does NOT silently rotate OIDC provider
# thumbprints; whatever Terraform commits is what apply uses. Computing
# the SHA-1 fingerprint from the live TLS handshake at plan time is the
# documented HashiCorp pattern for keeping the trust intact across cert
# rotations (the alternative is a hand-pinned list that bit-rots and
# silently breaks the trust the next time the issuer rotates).
data "tls_certificate" "github_oidc" {
  url = "https://token.actions.githubusercontent.com"
}

data "tls_certificate" "tfc_oidc" {
  url = "https://app.terraform.io"
}

###############################################################################
# S3 BUCKET
# Private (block public access on all four flags), versioned, SSE-S3.
# CloudFront reaches it via OAC; no public read path exists.
###############################################################################

resource "aws_s3_bucket" "apex" {
  bucket = local.bucket_name

  tags = {
    Name = local.bucket_name
    role = "apex-static-origin"
  }
}

resource "aws_s3_bucket_public_access_block" "apex" {
  bucket = aws_s3_bucket.apex.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "apex" {
  bucket = aws_s3_bucket.apex.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "apex" {
  bucket = aws_s3_bucket.apex.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_ownership_controls" "apex" {
  bucket = aws_s3_bucket.apex.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "apex" {
  bucket = aws_s3_bucket.apex.id

  # versioning_configuration above must apply before lifecycle rules that
  # reference noncurrent_version_expiration; depends_on makes the order
  # explicit so terraform plan doesn't race.
  depends_on = [aws_s3_bucket_versioning.apex]

  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_expiration_days
    }

    # Abort multipart uploads left behind by a failed deploy after 7 days.
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # Reap hashed _next/static/ chunks orphaned by a Next.js rename. The deploy
  # uploads new hashed assets without --delete (PR #267, to avoid 404ing
  # browser-cached HTML mid-deploy), so renamed chunks keep a distinct, current
  # key forever and the noncurrent-version rule above never touches them. Every
  # in-use chunk is re-uploaded on each deploy, refreshing its mtime, so this
  # expiration only reaps chunks absent from recent build output.
  rule {
    id     = "expire-orphaned-next-static"
    status = "Enabled"

    filter {
      prefix = "_next/static/"
    }

    expiration {
      days = var.orphaned_static_expiration_days
    }
  }
}

###############################################################################
# CLOUDFRONT ACCESS LOGS BUCKET
# Dedicated, private bucket for CloudFront standard (legacy) access logs.
#
# CloudFront standard logging delivers log files using an ACL grant to the
# `awslogsdelivery` AWS-owned account (the canonical-user-id path), NOT a
# service-principal bucket policy. That means this bucket MUST have ACLs
# enabled — object_ownership = "BucketOwnerPreferred", and the account-level
# Block Public Access must leave `block_public_acls` / `ignore_public_acls`
# OFF so the delivery ACL grant lands. (The origin bucket uses the stricter
# BucketOwnerEnforced + full BPA; these two buckets intentionally differ.)
# The delivery grant is set EXPLICITLY via `aws_s3_bucket_acl.apex_logs`
# rather than relying on the deprecated implicit auto-grant, which can fail
# at apply with InvalidArgument.
###############################################################################

resource "aws_s3_bucket" "apex_logs" {
  bucket = local.logs_bucket_name

  tags = {
    Name = local.logs_bucket_name
    role = "apex-cloudfront-logs"
  }

  # Gate creation behind the IAM-propagation window. The provisioner role's
  # inline policy is widened to allow s3:CreateBucket on this bucket in the
  # SAME apply; without this dependency Terraform races the policy update and
  # CreateBucket 403s while the assumed-role session still sees the old policy
  # (same eventual-consistency failure the Route 53 writes hit on PR #270).
  # No cycle: the policy references this bucket by constructed ARN string, not
  # by resource attribute, so there is no policy -> bucket edge to close.
  depends_on = [time_sleep.iam_policy_propagation]
}

# ACLs enabled. CloudFront standard log delivery writes via an ACL grant to
# the log-delivery account; BucketOwnerEnforced (which disables ACLs) would
# reject the grant and break log delivery. BucketOwnerPreferred keeps the
# bucket owner as the owner of delivered objects while still permitting ACLs.
resource "aws_s3_bucket_ownership_controls" "apex_logs" {
  bucket = aws_s3_bucket.apex_logs.id

  rule {
    object_ownership = "BucketOwnerPreferred"
  }
}

# Block PUBLIC access while still permitting the CloudFront log-delivery ACL
# grant. The two *_acls flags are left false because disabling them entirely
# (as the origin bucket does) would also block the non-public delivery grant.
# block_public_policy + restrict_public_buckets stay on, so no public policy
# or public bucket exposure is possible regardless of the ACL grant.
resource "aws_s3_bucket_public_access_block" "apex_logs" {
  bucket = aws_s3_bucket.apex_logs.id

  block_public_acls       = false
  block_public_policy     = true
  ignore_public_acls      = false
  restrict_public_buckets = true
}

# CloudFront standard logging delivers via an ACL grant to a fixed AWS-owned
# canonical user (the log-delivery account), not a service principal. The
# deprecated implicit behavior where enabling logging_config auto-grants this
# can fail at apply with InvalidArgument, so we grant it explicitly here.
# `aws_canonical_user_id` resolves the calling account's canonical id so the
# bucket OWNER keeps FULL_CONTROL alongside the log-delivery grant.
data "aws_canonical_user_id" "current" {}

# CloudFront standard-logging delivery canonical user id (a fixed, global,
# AWS-published value). FULL_CONTROL lets the log-delivery account write and
# set ACLs on the log objects it delivers.
locals {
  cloudfront_log_delivery_canonical_id = "c4c1ede66af53448b93c283ce9448c4ba468c9432aa01d700d3878632f77d2d0"
}

# Explicit ACL granting the bucket owner FULL_CONTROL and the CloudFront
# log-delivery account FULL_CONTROL. This replaces the deprecated implicit
# auto-grant that logging_config used to perform on enable.
resource "aws_s3_bucket_acl" "apex_logs" {
  bucket = aws_s3_bucket.apex_logs.id

  access_control_policy {
    owner {
      id = data.aws_canonical_user_id.current.id
    }

    grant {
      grantee {
        type = "CanonicalUser"
        id   = data.aws_canonical_user_id.current.id
      }
      permission = "FULL_CONTROL"
    }

    grant {
      grantee {
        type = "CanonicalUser"
        id   = local.cloudfront_log_delivery_canonical_id
      }
      permission = "FULL_CONTROL"
    }
  }

  # Ownership controls (ACLs enabled) and the public-access-block must exist
  # before the ACL is applied, and the ACL must exist before CloudFront's first
  # log delivery (sequenced via the distribution's depends_on).
  depends_on = [
    aws_s3_bucket_ownership_controls.apex_logs,
    aws_s3_bucket_public_access_block.apex_logs,
  ]
}

resource "aws_s3_bucket_server_side_encryption_configuration" "apex_logs" {
  bucket = aws_s3_bucket.apex_logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "apex_logs" {
  bucket = aws_s3_bucket.apex_logs.id

  rule {
    id     = "expire-access-logs"
    status = "Enabled"

    filter {
      prefix = "apex/"
    }

    expiration {
      days = var.access_log_expiration_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

###############################################################################
# ACM CERTIFICATE (us-east-1, CloudFront requirement)
# DNS-validated via the existing Route 53 zone. Validation records are
# automatically managed; they're scoped to ACM's _<random>.<domain> CNAMEs
# and DO NOT touch the apex A record.
###############################################################################

resource "aws_acm_certificate" "apex" {
  provider = aws.us_east_1

  domain_name               = local.apex_fqdn
  subject_alternative_names = [local.www_fqdn]
  validation_method         = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = {
    Name = "${local.apex_fqdn}-cf"
  }
}

# Route 53 validation records. ACM emits one CNAME per (domain, SAN) pair;
# the for_each loop materialises them. These records are _<token>.<domain>
# style and do NOT collide with the apex A record (PR-D's territory).
resource "aws_route53_record" "apex_acm_validation" {
  for_each = {
    for dvo in aws_acm_certificate.apex.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }

  allow_overwrite = true
  name            = each.value.name
  records         = [each.value.record]
  ttl             = 60
  type            = each.value.type
  zone_id         = data.aws_route53_zone.apex.zone_id

  # The WriteAcmValidationCnames IAM statement (further down in this
  # file) pins the allowed CNAME names to exactly what ACM exposes via
  # aws_acm_certificate.apex.domain_validation_options. If the cert
  # rotates (new SAN, recreate) the policy values change. The
  # time_sleep.iam_policy_propagation gate (defined alongside the IAM
  # resource) sequences this record after both the policy modification
  # AND the IAM eventual-consistency window, so a same-run apply does
  # not 403 against a cached pre-update policy.
  depends_on = [time_sleep.iam_policy_propagation]
}

resource "aws_acm_certificate_validation" "apex" {
  provider = aws.us_east_1

  certificate_arn         = aws_acm_certificate.apex.arn
  validation_record_fqdns = [for r in aws_route53_record.apex_acm_validation : r.fqdn]
}

# Apex cutover (L5.2a PR-D). A and AAAA ALIAS records pointing the apex
# and www hostnames at the CloudFront distribution. Both record types
# are needed because the distribution has is_ipv6_enabled = true; an
# IPv6-only client otherwise cannot resolve the apex.
#
# The www records point at the SAME distribution (not at the apex name)
# because CloudFront has both hostnames in its `aliases` list and the
# viewer-request function performs the www -> apex 301 redirect at the
# edge after the TLS handshake. Sending www to CloudFront first lets the
# redirect happen with HTTPS already established; sending www somewhere
# else (e.g. CNAME to apex) would force a second DNS lookup and an
# extra TLS handshake for every www visitor.
#
# evaluate_target_health = false is required for CloudFront aliases;
# CloudFront does its own health checking internally.
#
# depends_on on time_sleep.iam_policy_propagation (defined alongside
# the IAM resource further down) is required for any same-run apply
# that widens the policy AND creates these records. Without it,
# Terraform parallelizes and writes the records before the updated
# policy has propagated through IAM, producing 403 "no identity-based
# policy allows" denials even though the put-role-policy call itself
# returned success. The time_sleep gate inserts a 30s grace window
# between the IAM mutation and any record write. On subsequent applies
# where the policy is unchanged, the gate is a no-op (sleep does not
# re-fire because its trigger hash is stable).

resource "aws_route53_record" "apex_a" {
  zone_id = data.aws_route53_zone.apex.zone_id
  name    = var.domain
  type    = "A"

  alias {
    name                   = aws_cloudfront_distribution.apex.domain_name
    zone_id                = aws_cloudfront_distribution.apex.hosted_zone_id
    evaluate_target_health = false
  }

  depends_on = [time_sleep.iam_policy_propagation]
}

resource "aws_route53_record" "apex_aaaa" {
  zone_id = data.aws_route53_zone.apex.zone_id
  name    = var.domain
  type    = "AAAA"

  alias {
    name                   = aws_cloudfront_distribution.apex.domain_name
    zone_id                = aws_cloudfront_distribution.apex.hosted_zone_id
    evaluate_target_health = false
  }

  depends_on = [time_sleep.iam_policy_propagation]
}

resource "aws_route53_record" "www_a" {
  zone_id = data.aws_route53_zone.apex.zone_id
  name    = "www.${var.domain}"
  type    = "A"

  alias {
    name                   = aws_cloudfront_distribution.apex.domain_name
    zone_id                = aws_cloudfront_distribution.apex.hosted_zone_id
    evaluate_target_health = false
  }

  depends_on = [time_sleep.iam_policy_propagation]
}

resource "aws_route53_record" "www_aaaa" {
  zone_id = data.aws_route53_zone.apex.zone_id
  name    = "www.${var.domain}"
  type    = "AAAA"

  alias {
    name                   = aws_cloudfront_distribution.apex.domain_name
    zone_id                = aws_cloudfront_distribution.apex.hosted_zone_id
    evaluate_target_health = false
  }

  depends_on = [time_sleep.iam_policy_propagation]
}

###############################################################################
# CLOUDFRONT. Origin Access Control (OAC, NOT legacy OAI), response-headers
# policy with HSTS et al., CloudFront Function for www -> apex 301 redirect.
###############################################################################

resource "aws_cloudfront_origin_access_control" "apex" {
  name                              = "${local.bucket_name}-oac"
  description                       = "OAC for apex landing static site."
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# Response headers policy: CSP, HSTS, X-Content-Type-Options, X-Frame-Options,
# Referrer-Policy, Permissions-Policy. Baseline web security headers for the
# apex static landing distribution. The CSP value is assembled in the locals
# block at the top of this file (local.apex_csp); see the rationale there for
# why it is intentionally distinct from the Next.js app's nonce-based CSP.
resource "aws_cloudfront_response_headers_policy" "apex" {
  name    = "${local.bucket_name}-security-headers"
  comment = "Baseline security headers for the apex landing distribution."

  security_headers_config {
    content_security_policy {
      content_security_policy = local.apex_csp
      override                = true
    }

    strict_transport_security {
      access_control_max_age_sec = 63072000 # 2 years
      include_subdomains         = true
      preload                    = true
      override                   = true
    }

    content_type_options {
      override = true
    }

    frame_options {
      frame_option = "DENY"
      override     = true
    }

    referrer_policy {
      referrer_policy = "strict-origin-when-cross-origin"
      override        = true
    }
  }

  custom_headers_config {
    items {
      header   = "Permissions-Policy"
      value    = "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()"
      override = true
    }

    # No Reporting-Endpoints header: the apex CSP reports violations via the
    # legacy report-uri directive only (see local.apex_csp). The modern
    # Reporting API (report-to + Reporting-Endpoints) is intentionally omitted
    # because a cross-origin reporting endpoint is CORS-gated and would require
    # adding the apex origin to the app backend's BACKEND_CORS_ORIGINS
    # allowlist, which is out of scope for this infra-only change.
  }
}

# CloudFront Function: single viewer-request handler combining
# (1) www -> apex 301 redirect and (2) directory-style URL rewrites
# (e.g. /privacy/ -> /privacy/index.html). CloudFront only allows one
# viewer-request function per behavior, so both behaviors live in this
# function with a strict order: REDIRECT runs first (so we don't waste
# work rewriting URIs for requests we're about to bounce), REWRITE runs
# second (so requests that survive the redirect have S3-resolvable URIs).
#
# Why rewrite is needed: with S3 + OAC (REST origin), requests for
# "/privacy/" hit S3 looking for an object literally named "privacy/" and
# get a 404/403. The S3 static-website-hosting endpoint translates this
# automatically, but it's public + non-HTTPS so we deliberately don't use
# it. PR-C's static export produces out-apex/privacy/index.html etc.;
# this function bridges the gap.
resource "aws_cloudfront_function" "viewer_request" {
  name    = "${replace(var.domain, ".", "-")}-viewer-request"
  runtime = "cloudfront-js-2.0"
  comment = "www->apex redirect + S3 directory index rewrite for ${var.domain}"
  publish = true

  code = <<-EOT
function handler(event) {
  var request = event.request;

  // 1) www -> apex 301 redirect. Runs FIRST so we never spend rewrite
  //    cycles on requests we're about to bounce to a different host.
  var host = request.headers.host && request.headers.host.value;
  if (host && host.toLowerCase() === "${local.www_fqdn}") {
    return {
      statusCode: 301,
      statusDescription: "Moved Permanently",
      headers: {
        "location": { "value": "https://${local.apex_fqdn}" + request.uri }
      }
    };
  }

  // 2) S3 directory index rewrite. Runs SECOND so it only applies to
  //    requests that survived the redirect check. "/privacy/" becomes
  //    "/privacy/index.html"; "/about" (no trailing slash, no extension)
  //    becomes "/about/index.html". Requests with an extension (".css",
  //    ".png", ".js") pass through untouched.
  var uri = request.uri;
  if (uri.endsWith("/")) {
    request.uri += "index.html";
  } else if (!uri.includes(".")) {
    request.uri += "/index.html";
  }
  return request;
}
EOT
}

resource "aws_cloudfront_distribution" "apex" {
  enabled             = true
  is_ipv6_enabled     = true
  comment             = "${var.domain} apex landing (L5.2a)"
  default_root_object = "index.html"
  price_class         = "PriceClass_100" # NA + EU PoPs; cheapest tier that covers target users.
  http_version        = "http2and3"

  aliases = [local.apex_fqdn, local.www_fqdn]

  # CloudFront standard (access) logging to the dedicated logs bucket. The
  # `bucket` argument expects the S3 bucket DOMAIN name (not the bare id or
  # ARN). Logs land under the apex/ prefix so the bucket can host other
  # CloudFront log streams later without key collisions, and so the lifecycle
  # rule (which filters on prefix = "apex/") matches. include_cookies is left
  # at its default (false): the apex is a cookieless static site, so logging
  # cookies would only add noise.
  logging_config {
    bucket = aws_s3_bucket.apex_logs.bucket_regional_domain_name
    prefix = "apex/"
  }

  origin {
    domain_name              = aws_s3_bucket.apex.bucket_regional_domain_name
    origin_id                = local.s3_origin_id
    origin_access_control_id = aws_cloudfront_origin_access_control.apex.id
  }

  default_cache_behavior {
    target_origin_id       = local.s3_origin_id
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    # AWS managed CachingOptimized policy (long TTL, gzip/br on,
    # query strings ignored). Matches the static-export pattern where
    # hashed asset filenames are the cache-busting key.
    cache_policy_id = "658327ea-f89d-4fab-a63d-7e88639e58f6"

    # AWS managed CORS-S3Origin: forwards Origin + the bare minimum for
    # cross-origin font loading without exploding the cache key.
    origin_request_policy_id = "88a5eaf4-2fd4-4709-b370-b4c650ea3fcf"

    response_headers_policy_id = aws_cloudfront_response_headers_policy.apex.id

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.viewer_request.arn
    }
  }

  custom_error_response {
    error_code            = 403
    response_code         = 404
    response_page_path    = "/404.html"
    error_caching_min_ttl = 60
  }

  custom_error_response {
    error_code            = 404
    response_code         = 404
    response_page_path    = "/404.html"
    error_caching_min_ttl = 60
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.apex.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }

  # The logs bucket's ownership controls (ACLs enabled), BPA settings, and the
  # explicit log-delivery ACL grant must all be in place before CloudFront
  # attempts its first log delivery; otherwise the delivery write is rejected
  # and the distribution create/update errors. Sequencing here makes that
  # explicit instead of relying on the implicit domain-name reference alone.
  depends_on = [
    aws_s3_bucket_ownership_controls.apex_logs,
    aws_s3_bucket_public_access_block.apex_logs,
    aws_s3_bucket_acl.apex_logs,
  ]

  tags = {
    Name = "${var.domain}-apex"
  }
}

###############################################################################
# S3 BUCKET POLICY. Grant CloudFront (via OAC) read on the bucket. Scoped to
# this distribution's ARN; no other principal gets access.
###############################################################################

data "aws_iam_policy_document" "apex_bucket" {
  statement {
    sid    = "AllowCloudFrontServicePrincipalReadOnly"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.apex.arn}/*"]

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.apex.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "apex" {
  bucket = aws_s3_bucket.apex.id
  policy = data.aws_iam_policy_document.apex_bucket.json

  # Public access block must apply BEFORE a bucket policy lands, else the
  # account-level BPA settings can race the policy evaluation.
  depends_on = [aws_s3_bucket_public_access_block.apex]
}

###############################################################################
# IAM OIDC PROVIDERS. GitHub Actions + Terraform Cloud workload identity.
# These are AWS-account-global resources; if either provider already exists
# in the account (e.g. from a different project), this module will conflict
# at plan time and the owner should `terraform import` the existing one
# instead of double-creating. The bootstrap notes in README.md cover this.
###############################################################################

# GitHub Actions OIDC. Thumbprints are computed at plan time from the live
# TLS handshake against token.actions.githubusercontent.com via the
# tls_certificate data source above. AWS does NOT auto-rotate OIDC
# provider thumbprints, so hand-pinning a list is fragile across issuer
# cert rotations; computing on apply keeps the trust intact.
resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.github_oidc.certificates[0].sha1_fingerprint]

  tags = {
    Name = "github-actions-oidc"
  }
}

# Terraform Cloud workload identity. Single-audience: aws.workload.identity.
# Thumbprint computed at plan time from app.terraform.io's live cert chain.
resource "aws_iam_openid_connect_provider" "tfc" {
  url             = "https://app.terraform.io"
  client_id_list  = ["aws.workload.identity"]
  thumbprint_list = [data.tls_certificate.tfc_oidc.certificates[0].sha1_fingerprint]

  tags = {
    Name = "tfc-workload-identity"
  }
}

###############################################################################
# IAM ROLE: github_actions_apex_deploy
# Assumable ONLY from GitHub Actions workflow runs whose OIDC token subject
# exactly equals `repo:${var.github_repo}:ref:refs/heads/${var.github_main_branch}`.
# This is the "push to main" subject; PR-context tokens have a different sub
# (`pull_request`) and cannot match.
#
# Why exact-match: workflow-level guards like `if: github.ref == 'refs/heads/main'`
# are not sufficient because anyone who can open a PR can also rewrite the
# workflow file in that PR and remove the guard. The trust policy itself
# must reject non-main subjects.
#
# Use `StringEquals` (not StringLike) on the sub claim. NO pull_request
# patterns. If PR previews are needed later, that requires a SEPARATE
# read-only role (see follow-up note in apex/README.md).
###############################################################################

data "aws_iam_policy_document" "github_actions_trust" {
  statement {
    sid     = "GitHubActionsFromMainOnly"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [local.github_main_sub]
    }
  }
}

resource "aws_iam_role" "github_actions_apex_deploy" {
  name                 = "github-actions-apex-deploy"
  description          = "Assumed by GitHub Actions (${var.github_repo}) to deploy the apex landing static export."
  assume_role_policy   = data.aws_iam_policy_document.github_actions_trust.json
  max_session_duration = 3600

  tags = {
    role = "github-actions-apex-deploy"
  }
}

# Inline policy: scoped to THIS bucket + THIS distribution. No * resources.
data "aws_iam_policy_document" "github_actions_deploy" {
  statement {
    sid       = "ListBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.apex.arn]
  }

  statement {
    sid    = "ReadWriteObjects"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = ["${aws_s3_bucket.apex.arn}/*"]
  }

  statement {
    sid    = "InvalidateDistribution"
    effect = "Allow"
    actions = [
      "cloudfront:CreateInvalidation",
      "cloudfront:GetInvalidation",
      "cloudfront:ListInvalidations",
    ]
    resources = [aws_cloudfront_distribution.apex.arn]
  }
}

resource "aws_iam_role_policy" "github_actions_apex_deploy" {
  name   = "github-actions-apex-deploy-inline"
  role   = aws_iam_role.github_actions_apex_deploy.id
  policy = data.aws_iam_policy_document.github_actions_deploy.json
}

###############################################################################
# IAM ROLE: tfc_apex_provisioner
# Assumable from TFC workload identity tokens originating in the pfv-apex
# workspace (or any workspace matching var.tfc_workspace_pattern). Has full
# management of THIS module's resources: S3 bucket, CloudFront distribution,
# ACM cert, IAM role chain, and the Route 53 records this module manages.
# Route 53 writes are narrowly scoped via two IAM condition pairs (see the
# WriteApexAndWwwAliasRecords and WriteAcmValidationCnames statements below)
# so the role can ONLY write A/AAAA on the apex+www names and CNAME on the
# ACM validation pattern, never anything else in the zone.
###############################################################################

data "aws_iam_policy_document" "tfc_trust" {
  statement {
    sid     = "TFCWorkloadIdentity"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.tfc.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "app.terraform.io:aud"
      values   = ["aws.workload.identity"]
    }

    condition {
      test     = "StringLike"
      variable = "app.terraform.io:sub"
      values   = [local.tfc_sub_pattern]
    }
  }
}

resource "aws_iam_role" "tfc_apex_provisioner" {
  name                 = "tfc-apex-provisioner"
  description          = "Assumed by TFC (${var.tfc_organization}/${var.tfc_workspace_pattern}) to provision apex infra."
  assume_role_policy   = data.aws_iam_policy_document.tfc_trust.json
  max_session_duration = 3600

  tags = {
    role = "tfc-apex-provisioner"
  }
}

data "aws_iam_policy_document" "tfc_apex_provisioner" {
  # S3 management on THIS module's buckets only: the static-origin bucket and
  # the dedicated CloudFront access-logs bucket.
  statement {
    sid    = "ManageApexBuckets"
    effect = "Allow"
    actions = [
      "s3:*",
    ]
    resources = [
      aws_s3_bucket.apex.arn,
      "${aws_s3_bucket.apex.arn}/*",
      # Referenced by constructed ARN (not aws_s3_bucket.apex_logs.arn) on
      # purpose: a resource reference would make this policy depend on the
      # logs bucket, so Terraform would create the bucket BEFORE the policy
      # that grants s3:CreateBucket is in effect -> AccessDenied on first
      # apply. With a static ARN the policy has no edge to the bucket, the
      # bucket instead depends on time_sleep.iam_policy_propagation, and the
      # ordering becomes: widen policy -> wait for IAM propagation -> create
      # bucket. The name is fixed (local.logs_bucket_name) so the ARN is
      # fully known at plan time.
      "arn:aws:s3:::${local.logs_bucket_name}",
      "arn:aws:s3:::${local.logs_bucket_name}/*",
    ]
  }

  # ListAllMyBuckets is account-wide and needed for some plan operations.
  statement {
    sid       = "ListAllBucketsForPlan"
    effect    = "Allow"
    actions   = ["s3:ListAllMyBuckets", "s3:GetBucketLocation"]
    resources = ["*"]
  }

  # CloudFront management on this distribution. CloudFront IAM is not
  # ARN-scoped on all actions (some, like CreateDistribution, only accept
  # "*"); we accept that limitation rather than splitting the policy.
  statement {
    sid    = "ManageApexDistribution"
    effect = "Allow"
    actions = [
      "cloudfront:*",
    ]
    resources = ["*"]
  }

  # ACM in us-east-1 for the cert. ACM IAM is region-keyed via resource ARN
  # so this scopes to certificates in us-east-1 within this account.
  statement {
    sid    = "ManageApexCertificate"
    effect = "Allow"
    actions = [
      "acm:*",
    ]
    resources = ["arn:aws:acm:us-east-1:${var.aws_account_id}:certificate/*"]
  }

  # Route 53 read access on the apex zone. The two ChangeResourceRecordSets
  # writes below (apex/www ALIAS + ACM validation CNAME) are narrowly scoped
  # by record name AND type via separate statements.
  statement {
    sid    = "ReadApexZone"
    effect = "Allow"
    actions = [
      "route53:GetHostedZone",
      "route53:ListHostedZones",
      "route53:ListHostedZonesByName",
      "route53:GetChange",
      "route53:ListResourceRecordSets",
      # data.aws_route53_zone calls ListTagsForResource as part of its
      # read since AWS provider v5.x. Without these, refresh fails with
      # 403 on every plan/apply that touches the data source. Both
      # singular and plural variants are distinct IAM permissions; grant
      # both so future provider changes that switch APIs do not regress.
      "route53:ListTagsForResource",
      "route53:ListTagsForResources",
    ]
    resources = ["*"]
  }

  # Route 53 write scope is split into two narrow statements. Each restricts
  # both record type AND record name, so the role cannot pivot to other
  # records in the zone even if an attacker reaches the OIDC role.
  #
  # AWS condition keys used here:
  #   route53:ChangeResourceRecordSetsRecordTypes  -> record type allowlist
  #   route53:ChangeResourceRecordSetsNormalizedRecordNames -> record name allowlist
  #   route53:ChangeResourceRecordSetsActions      -> CREATE/UPSERT/DELETE
  # The "Normalized" name comparison is case-insensitive and trims any trailing
  # dot, so values are written here as the bare FQDNs.

  # Statement 1: apex + www ALIAS records, A and AAAA only. The two ALIAS
  # records pointing at the apex CloudFront distribution are the cutover.
  statement {
    sid    = "WriteApexAndWwwAliasRecords"
    effect = "Allow"
    actions = [
      "route53:ChangeResourceRecordSets",
    ]
    resources = ["arn:aws:route53:::hostedzone/${data.aws_route53_zone.apex.zone_id}"]

    condition {
      test     = "ForAllValues:StringEquals"
      variable = "route53:ChangeResourceRecordSetsRecordTypes"
      values   = ["A", "AAAA"]
    }

    condition {
      test     = "ForAllValues:StringEquals"
      variable = "route53:ChangeResourceRecordSetsNormalizedRecordNames"
      values = [
        var.domain,
        "www.${var.domain}",
      ]
    }
  }

  # Statement 2: ACM DNS-validation CNAMEs. ACM emits CNAMEs at exact
  # names exposed in aws_acm_certificate.apex.domain_validation_options.
  # AWS reuses these names across cert renewals, so they are stable for
  # the life of the cert (and re-derive automatically on plan if a SAN
  # is added or the cert is recreated).
  #
  # Earlier revision used StringLike on "_*.<domain>", but IAM string
  # wildcards are NOT DNS-label-bounded: "_*.thebetterdecision.com"
  # would also match "_acme-challenge.foo.thebetterdecision.com" and
  # any other underscore-prefixed name elsewhere in the zone. Pinning
  # to the exact names ACM is currently asking for removes that gap.
  #
  # NormalizedRecordNames comparison is case-insensitive; AWS lowercases
  # and trims any trailing dot before evaluating. We pre-normalize here
  # so the rendered policy matches what AWS will compare against.
  statement {
    sid    = "WriteAcmValidationCnames"
    effect = "Allow"
    actions = [
      "route53:ChangeResourceRecordSets",
    ]
    resources = ["arn:aws:route53:::hostedzone/${data.aws_route53_zone.apex.zone_id}"]

    condition {
      test     = "ForAllValues:StringEquals"
      variable = "route53:ChangeResourceRecordSetsRecordTypes"
      values   = ["CNAME"]
    }

    condition {
      test     = "ForAllValues:StringEquals"
      variable = "route53:ChangeResourceRecordSetsNormalizedRecordNames"
      values = [
        for dvo in aws_acm_certificate.apex.domain_validation_options :
        trimsuffix(lower(dvo.resource_record_name), ".")
      ]
    }
  }

  # IAM management for this role chain (self-management) + the OIDC
  # providers. Scoped to the apex-related resource names.
  statement {
    sid    = "ManageApexIamRoles"
    effect = "Allow"
    actions = [
      "iam:*Role*",
      "iam:*RolePolic*",
      "iam:PassRole",
      "iam:TagRole",
      "iam:UntagRole",
    ]
    resources = [
      "arn:aws:iam::${var.aws_account_id}:role/github-actions-apex-deploy",
      "arn:aws:iam::${var.aws_account_id}:role/tfc-apex-provisioner",
    ]
  }

  statement {
    sid    = "ManageOidcProviders"
    effect = "Allow"
    actions = [
      "iam:*OpenIDConnectProvider*",
    ]
    resources = [
      "arn:aws:iam::${var.aws_account_id}:oidc-provider/token.actions.githubusercontent.com",
      "arn:aws:iam::${var.aws_account_id}:oidc-provider/app.terraform.io",
    ]
  }
}

resource "aws_iam_role_policy" "tfc_apex_provisioner" {
  name   = "tfc-apex-provisioner-inline"
  role   = aws_iam_role.tfc_apex_provisioner.id
  policy = data.aws_iam_policy_document.tfc_apex_provisioner.json
}

# Same-run grace window between an IAM policy update and any Route 53
# write made by the assumed role. AWS IAM is eventually consistent: the
# put-role-policy call returns success within milliseconds, but the
# assumed-role session evaluating subsequent requests can still see the
# OLD policy for several seconds afterward. Without this grace, a
# same-run apply that widens the policy AND creates a record can fail
# the record creation with "no identity-based policy allows", because
# the role session is still using the pre-update policy.
#
# This was the failure mode on the first PR #270 apply (run
# Fd4y4Y5JyFkKQoLL, 2026-05-14): the IAM update completed at 13:46:20.45
# and the 4 Route 53 creates all 403'd between 13:46:20.64 and 13:46:20.95.
#
# Triggers on a hash of the rendered policy JSON so the sleep only
# re-fires when the policy actually changes; steady-state applies are
# no-ops here. Duration is the AWS-recommended starting point for IAM
# propagation in same-account / same-region patterns.
resource "time_sleep" "iam_policy_propagation" {
  triggers = {
    policy_hash = sha256(aws_iam_role_policy.tfc_apex_provisioner.policy)
  }

  create_duration = "30s"
}
