###############################################################################
# pfv apex landing: S3 (private) + CloudFront (OAC) + ACM (us-east-1) +
# IAM OIDC roles (GitHub Actions deploy, TFC apex provisioner).
#
# PR-A of the L5.2a apex split. NO Route 53 A-record changes here. The apex
# stays parked through this PR. ACM DNS-validation records ARE written into
# the existing Route 53 zone (they don't touch the apex itself, only the
# _acme-challenge style validation CNAMEs ACM emits).
#
# Cutover (apex A ALIAS swap, TTL drop) is PR-D. PR-B consumes
# github_actions_role_arn from outputs.tf; PR-C produces the static export
# that gets s3-synced into the bucket.
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
}

resource "aws_acm_certificate_validation" "apex" {
  provider = aws.us_east_1

  certificate_arn         = aws_acm_certificate.apex.arn
  validation_record_fqdns = [for r in aws_route53_record.apex_acm_validation : r.fqdn]
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

# Response headers policy: HSTS, X-Content-Type-Options, X-Frame-Options,
# Referrer-Policy, Permissions-Policy. These are baseline web security
# headers; CSP is intentionally omitted from this PR because the static
# export's CSP needs to be authored alongside PR-C's build output.
resource "aws_cloudfront_response_headers_policy" "apex" {
  name    = "${local.bucket_name}-security-headers"
  comment = "Baseline security headers for the apex landing distribution."

  security_headers_config {
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
# ACM cert, IAM role chain. Route 53 access is READ-ONLY (Get* on the apex
# zone) so PR-A cannot accidentally write A records; PR-D adds the write
# permissions when the cutover Terraform lands.
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
  # S3 management on THIS bucket only.
  statement {
    sid    = "ManageApexBucket"
    effect = "Allow"
    actions = [
      "s3:*",
    ]
    resources = [
      aws_s3_bucket.apex.arn,
      "${aws_s3_bucket.apex.arn}/*",
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

  # Route 53 READ-ONLY on the apex zone. ACM validation CNAMEs and the
  # data lookup need Get/List; PR-A intentionally CANNOT write A records.
  # PR-D will widen this to ChangeResourceRecordSets when cutover lands.
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

  # ACM validation creates _<token>.<domain> CNAMEs in the zone. Without
  # write access to the zone, PR-A's apply cannot complete. The
  # ForAllValues:StringEquals condition on route53:ChangeResourceRecordSetsRecordTypes
  # restricts the role to CNAME writes ONLY. The apex A record (and any
  # other record type) is unreachable through this role. PR-D will widen
  # the condition to add "A" when it ships the apex ALIAS swap. This
  # makes "no A records in PR-A" an IAM-enforced invariant, not just a
  # Terraform code-discipline one.
  statement {
    sid    = "WriteAcmValidationRecordsCnameOnly"
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
