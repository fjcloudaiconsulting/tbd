data "aws_caller_identity" "current" {}

locals {
  bucket_arn = "arn:aws:s3:::${var.bucket_name}"

  # ⚠ Both the ORGANIZATION and the WORKSPACE segment are checked. An earlier
  # version built this string and never referenced it, so the org segment was
  # unverified on every path -- a trust document naming a different org would
  # have applied cleanly and locked the workspace out the TBD-372 way.
  tfc_sub_fragment = "organization:${var.tfc_organization}:project:*:workspace:${var.tfc_workspace_name}:run_phase:"
}

# ---------------------------------------------------------------------------
# Trust anchor.
#
# ⚠ These two resources were created BY HAND with root at genesis, from the
# committed documents in infra/aws/bootstrap/, then `terraform import`ed. That
# sequence is unavoidable: the account was empty, so root was the only principal
# that could mint the first role, and a workspace cannot assume a role that does
# not exist yet.
#
# They are Terraform-managed from here on, because the operator's standing rule
# is that we do not want infra without code -- a hand-applied IAM role whose
# drift no plan ever shows is exactly what that rule forbids.
#
# ⚠ That leaves the role self-authorizing: this workspace manages the trust
# policy that admits this workspace. That is the TBD-372 shape. Three defences,
# in increasing order of value:
#   1. backend/tests/test_backup_trust_anchor.py asserts, at PR time, that the
#      workspace named in versions.tf equals the one in the trust document
#      (backend/tests/test_backup_offhost.py). That
#      is what PREVENTS the event -- TBD-372 happened because a rename was
#      APPLIED with the pattern unchanged.
#   2. prevent_destroy below, so state surgery or a -target mistake cannot
#      remove the anchor.
#   3. The committed JSON is the source of truth, so recovery is "re-apply the
#      file", not "reconstruct the policy from memory under pressure".
# ---------------------------------------------------------------------------
resource "aws_iam_openid_connect_provider" "tfc" {
  url            = "https://app.terraform.io"
  client_id_list = ["aws.workload.identity"]
  # ⚠ AWS validates OIDC providers on well-known public CAs against its own
  # trust store, so this value is not load-bearing; it is required by the API.
  # Do not hand-maintain it as though it were a pin.
  thumbprint_list = ["9e99a48a9960b14926bb7f3b02e22da2b0ab7280"]

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_iam_role" "tfc_backups_provisioner" {
  name                 = "tfc-backups-provisioner"
  description          = "Assumed by TFC workspace FlamaCorp/tbd-backups via OIDC. Trust doc is committed at infra/aws/bootstrap/tfc-backups-trust.json."
  assume_role_policy   = file("${path.module}/../../aws/bootstrap/tfc-backups-trust.json")
  max_session_duration = 3600

  lifecycle {
    prevent_destroy = true

    # ⚠ Fails the PLAN if the committed trust document stops naming the
    # workspace this configuration declares. Belt to the PR-time fence's braces:
    # the fence catches it in review, this catches it if someone applies from a
    # branch that skipped review.
    precondition {
      condition     = can(regex(replace(local.tfc_sub_fragment, ".", "\\."), file("${path.module}/../../aws/bootstrap/tfc-backups-trust.json")))
      error_message = "The committed trust document does not authorize '${local.tfc_sub_fragment}...'. Applying would deny this workspace its own role (TBD-372). Widen the pattern, apply, rename, then narrow."
    }
  }
}

# ⚠⚠ SEPARATE PLAN AND APPLY ROLES, AND THIS IS A SECURITY BOUNDARY, NOT TIDINESS.
#
# HCP Terraform runs a SPECULATIVE PLAN on every PR touching this directory,
# before any approval and before any Confirm & Apply. A single role trusted at
# `run_phase:*` and holding s3:*/kms:*/iam:* would therefore let an UNMERGED PR
# assume account-admin and read production backups -- the PR's own HCL decides
# what runs at plan time. "Auto-apply is off" governs apply; it does not govern
# plan.
#
# So: this role is trusted only at run_phase:apply, and a separate read-only
# role is trusted at run_phase:plan. Set them as TFC_AWS_APPLY_ROLE_ARN and
# TFC_AWS_PLAN_ROLE_ARN respectively.
#
# ⚠ BOTH policies additionally carry an explicit Deny on s3:GetObject and
# kms:Decrypt. Terraform manages the bucket; it never needs to read a backup's
# CONTENT. That Deny means even an approved apply cannot decrypt customer data.
resource "aws_iam_role" "tfc_backups_plan" {
  name                 = "tfc-backups-plan"
  description          = "Assumed by TFC at PLAN phase only. Read-only, and explicitly denied any path to backup content."
  assume_role_policy   = file("${path.module}/../../aws/bootstrap/tfc-backups-plan-trust.json")
  max_session_duration = 3600

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_iam_role_policy" "tfc_backups_plan" {
  name   = "tfc-backups-plan-read-only"
  role   = aws_iam_role.tfc_backups_plan.id
  policy = file("${path.module}/../../aws/bootstrap/tfc-backups-plan.json")
}

resource "aws_iam_role_policy" "tfc_backups_provisioner" {
  name   = "tfc-backups-provisioner-inline"
  role   = aws_iam_role.tfc_backups_provisioner.id
  policy = file("${path.module}/../../aws/bootstrap/tfc-backups-provisioner.json")
}

# ---------------------------------------------------------------------------
# Encryption key.
# ---------------------------------------------------------------------------
resource "aws_kms_key" "backups" {
  description             = "Encrypts the off-host MySQL backups. TBD-400."
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.kms.json

  # ⚠ Without this the bucket can outlive its only key. A -target mistake or a
  # forced replacement schedules key deletion; 30 days later every object that
  # prevent_destroy and Object Lock successfully protected is permanently
  # unreadable -- and the freshness probe reports `fresh` throughout, because it
  # reads metadata and never touches KMS.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_alias" "backups" {
  name          = "alias/tbd-mysql-backups"
  target_key_id = aws_kms_key.backups.key_id
}

data "aws_iam_policy_document" "kms" {
  # ⚠ ROOT DELEGATION IS KEPT ON PURPOSE. Omitting it is the classic KMS
  # lockout footgun -- a key nobody can administer -- and it would be especially
  # reckless in an account whose root access keys are deleted at the end of this
  # rollout. The independence we want does NOT come from removing this.
  statement {
    sid       = "AccountAdministration"
    effect    = "Allow"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${var.aws_account_id}:root"]
    }
  }

  # ⚠ THIS is where the independence comes from. An explicit Deny in a KEY
  # policy cannot be overridden by any IAM policy or any bucket policy, so
  # neither the droplet nor the probe can ever be granted read of the plaintext,
  # however sloppy a future identity policy gets. That is the property SSE-S3
  # cannot offer at any price: with AES256, s3:GetObject IS plaintext.
  #
  # Conditioned on aws:PrincipalArn rather than a Principal block so the policy
  # does not fail to apply while those identities are still being created in
  # this same run.
  statement {
    sid       = "BackupWritersMayNeverDecrypt"
    effect    = "Deny"
    actions   = ["kms:Decrypt", "kms:ReEncryptFrom"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:PrincipalArn"
      values = [
        "arn:aws:iam::${var.aws_account_id}:user/pfv-backup-uploader",
        "arn:aws:iam::${var.aws_account_id}:role/github-actions-backup-probe",
      ]
    }
  }
}

# ---------------------------------------------------------------------------
# The bucket.
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "backups" {
  bucket = var.bucket_name

  # ⚠ Object Lock can ONLY be enabled at creation. Retrofitting it means a new
  # bucket and a migration, so it is decided here even though nothing depends on
  # it on day one. The credential that writes these objects lives on the very
  # box being defended, so a compromised droplet must not be able to overwrite
  # history -- versioning alone does not stop overwrite-in-place of the current
  # version.
  object_lock_enabled = true

  lifecycle {
    prevent_destroy = true

    precondition {
      condition     = data.aws_caller_identity.current.account_id == var.aws_account_id
      error_message = "Refusing to apply: caller is account ${data.aws_caller_identity.current.account_id} but aws_account_id is ${var.aws_account_id}. This repo spans two AWS accounts and apex uses the other one."
    }

    # ⚠ The earlier version of this message claimed expiration would "silently
    # fail on every object" if it raced Object Lock. That cannot happen:
    # expiration on a versioned bucket only writes a DELETE MARKER, which Object
    # Lock never blocks. The value that genuinely has to clear the lock is when
    # the noncurrent version is purged, and the guard belongs there.
    precondition {
      condition     = var.retention_days > var.object_lock_days
      error_message = "retention_days (${var.retention_days}) must exceed object_lock_days (${var.object_lock_days}); otherwise a version is purged while still under Object Lock retention and the purge is refused."
    }
  }
}

resource "aws_s3_bucket_versioning" "backups" {
  bucket = aws_s3_bucket.backups.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  rule {
    default_retention {
      # GOVERNANCE, not COMPLIANCE. COMPLIANCE is a one-way door that not even
      # root can undo for the retention period, which is the wrong trade for a
      # 7-day operational backup; GOVERNANCE lets a break-glass principal with
      # s3:BypassGovernanceRetention clean up a mistake.
      mode = "GOVERNANCE"
      days = var.object_lock_days
    }
  }

  depends_on = [aws_s3_bucket_versioning.backups]
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.backups.arn
    }
    # Cuts KMS request cost to roughly one call per upload rather than one per
    # object; the nightly volume is trivial either way, but it is free.
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket                  = aws_s3_bucket.backups.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  # ⚠ WITH VERSIONING ON, A BARE `expiration` RULE DELETES NOTHING. It writes a
  # delete marker and leaves the bytes as a noncurrent version, so a stated
  # retention window becomes fiction and storage grows without bound. All four
  # rules below are needed for "7 days" to mean 7 days.
  rule {
    id     = "expire-backups"
    status = "Enabled"

    filter {}

    expiration {
      days = var.retention_days
    }

    # ⚠ 1, not retention_days. Keys are date-partitioned so nothing is ever
    # overwritten; a version only becomes noncurrent when the expiration rule
    # above puts a delete marker over it on day 8. Counting another 8 days from
    # THERE meant bytes actually left at day 16, not day 8 -- twice the stated
    # retention, silently. The 7-day Object Lock has long expired by then.
    noncurrent_version_expiration {
      noncurrent_days = 1
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  rule {
    id     = "reap-delete-markers"
    status = "Enabled"

    filter {}

    expiration {
      expired_object_delete_marker = true
    }
  }

  depends_on = [aws_s3_bucket_versioning.backups]
}

data "aws_iam_policy_document" "bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [local.bucket_arn, "${local.bucket_arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  # ⚠ Defence in depth against a future widening of the uploader's IDENTITY
  # policy. The identity policy already omits these actions; this makes them
  # unreachable even if someone adds them there.
  statement {
    sid    = "DataPlaneIsWriteOnly"
    effect = "Deny"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "s3:ListBucket",
      "s3:ListBucketVersions",
    ]
    resources = [local.bucket_arn, "${local.bucket_arn}/*"]
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:PrincipalArn"
      values   = [aws_iam_user.uploader.arn]
    }
  }

  statement {
    sid    = "ProbeIsListOnly"
    effect = "Deny"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
    ]
    resources = [local.bucket_arn, "${local.bucket_arn}/*"]
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:PrincipalArn"
      values   = [aws_iam_role.backup_probe.arn]
    }
  }

  # ⚠ NOTE what is deliberately absent: no Deny naming the provisioner role.
  # That is the lockout shape -- a bucket policy that denies the only principal
  # able to rewrite the bucket policy.
}

resource "aws_s3_bucket_policy" "backups" {
  bucket = aws_s3_bucket.backups.id
  policy = data.aws_iam_policy_document.bucket.json

  depends_on = [aws_s3_bucket_public_access_block.backups]
}

# ---------------------------------------------------------------------------
# The droplet's put-only identity.
#
# ⚠ A long-lived key is used because the droplet has no OIDC identity and
# IAM Roles Anywhere would need a PKI and a certificate on the box -- far more
# machinery than a 620 KB nightly PUT justifies. The mitigation is that the key
# can only PUT and can only ENCRYPT: it cannot read, list, delete, or decrypt a
# single byte of what it wrote. A stolen key is a nuisance, not a breach.
# ---------------------------------------------------------------------------
resource "aws_iam_user" "uploader" {
  name = "pfv-backup-uploader"
}

resource "aws_iam_user_policy" "uploader" {
  name = "pfv-backup-uploader-put-only"
  user = aws_iam_user.uploader.name

  # Loaded from a committed JSON rather than written inline, so
  # backend/tests/test_backup_offhost.py can json.load it and assert
  # the action set in BOTH directions. A regex over HCL could not.
  policy = templatefile("${path.module}/policies/backup-uploader.json", {
    bucket      = var.bucket_name
    prefix      = var.backup_prefix
    kms_key_arn = aws_kms_key.backups.arn
  })
}

resource "aws_iam_access_key" "uploader" {
  user = aws_iam_user.uploader.name
}

# ---------------------------------------------------------------------------
# The off-host freshness probe's read-only identity.
# ---------------------------------------------------------------------------
resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

data "aws_iam_policy_document" "probe_trust" {
  statement {
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
    # ⚠ StringEquals on the exact branch ref, not StringLike. A PR context
    # cannot assume this role even if its author rewrites the workflow file in
    # that same PR -- the same posture apex/main.tf takes, for the same reason.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repo}:ref:refs/heads/${var.github_main_branch}"]
    }
  }
}

resource "aws_iam_role" "backup_probe" {
  name               = "github-actions-backup-probe"
  description        = "Assumed by the scheduled backup-freshness workflow. Lists object metadata; can read no object content."
  assume_role_policy = data.aws_iam_policy_document.probe_trust.json
}

resource "aws_iam_role_policy" "backup_probe" {
  name = "github-actions-backup-probe-list-only"
  role = aws_iam_role.backup_probe.id

  policy = templatefile("${path.module}/policies/backup-probe.json", {
    bucket = var.bucket_name
    prefix = var.backup_prefix
  })
}
