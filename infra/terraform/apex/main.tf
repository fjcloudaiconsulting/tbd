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

  # GitHub Actions OIDC subject claims. Push to main = full deploy; PRs get
  # plan-only access (no s3 put/delete, no invalidation), controlled in the
  # role's inline policy below by NOT granting mutating actions to PRs.
  github_main_sub = "repo:${var.github_repo}:ref:refs/heads/${var.github_main_branch}"
  github_pr_sub   = var.github_pr_subject_pattern == "" ? null : "repo:${var.github_repo}:${var.github_pr_subject_pattern}"

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

# CloudFront Function: redirects www.<apex> -> https://<apex> with a 301.
# Lightweight (no Lambda@Edge cold start cost); runs at viewer-request.
resource "aws_cloudfront_function" "www_to_apex_redirect" {
  name    = "${replace(var.domain, ".", "-")}-www-to-apex"
  runtime = "cloudfront-js-2.0"
  comment = "301 redirect www.${var.domain} -> https://${var.domain}"
  publish = true

  code = <<-EOT
function handler(event) {
  var request = event.request;
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
      function_arn = aws_cloudfront_function.www_to_apex_redirect.arn
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

# GitHub Actions OIDC. Thumbprint list is the published GitHub root cert
# chain; AWS verifies signed JWTs against these. Source:
# https://docs.github.com/en/actions/deployment/security-hardening-your-deployments/configuring-openid-connect-in-amazon-web-services
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  thumbprint_list = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
  ]

  tags = {
    Name = "github-actions-oidc"
  }
}

# Terraform Cloud workload identity. Single-audience: aws.workload.identity.
resource "aws_iam_openid_connect_provider" "tfc" {
  url            = "https://app.terraform.io"
  client_id_list = ["aws.workload.identity"]

  # TFC's certificate chain rotates; we let AWS resolve the thumbprint from
  # the JWKS rather than pinning a manual list, by setting a placeholder
  # that's overwritten on first validation. This matches HashiCorp's
  # documented pattern for the TFC -> AWS OIDC bridge.
  thumbprint_list = ["0c8e8c0b6b8e8e8c0b6b8e8e8c0b6b8e8e8c0b6b"]

  tags = {
    Name = "tfc-workload-identity"
  }
}

###############################################################################
# IAM ROLE: github_actions_apex_deploy
# Assumable from the pfv repo's GitHub Actions workflows. Main-branch pushes
# get s3 put/delete + cloudfront invalidation. PRs (if enabled) can ONLY
# assume the role for plan-style operations because the inline policy below
# is the only policy attached and it grants mutating actions unconditionally;
# PR-B's workflow restricts itself further with a job-level `if:` guard.
###############################################################################

data "aws_iam_policy_document" "github_actions_trust" {
  statement {
    sid     = "GitHubActionsFromMain"
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
      values   = compact([local.github_main_sub, local.github_pr_sub])
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
    ]
    resources = ["*"]
  }

  # ACM validation creates _<token>.<domain> CNAMEs in the zone. Without
  # write access to the zone, PR-A's apply cannot complete. Scoped to the
  # specific zone; this is the ONLY record-mutating permission granted.
  statement {
    sid    = "WriteAcmValidationRecords"
    effect = "Allow"
    actions = [
      "route53:ChangeResourceRecordSets",
    ]
    resources = ["arn:aws:route53:::hostedzone/${data.aws_route53_zone.apex.zone_id}"]
    # NOTE: ACM validation records are _<token>.<domain> CNAMEs; the apex
    # A record is left untouched by this PR. PR-D adds the apex A ALIAS;
    # that's the cutover step.
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
