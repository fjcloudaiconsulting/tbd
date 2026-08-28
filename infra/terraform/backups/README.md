# `tbd-backups` — off-host MySQL backup (TBD-400)

Owns the S3 bucket, CMK and IAM identities behind the nightly off-host copy of
the production MySQL dump.

⚠⚠ **This is a DIFFERENT AWS account from `infra/terraform/apex/`.** apex runs
the public landing site from the operator's older account; this workspace
targets the company account `884686184019`. `main.tf` asserts the caller matches
`var.aws_account_id`, so a wrong-account apply dies at plan instead of creating
a bucket in the wrong place. Never copy an account id between the two.

## Why a separate workspace

Folding these resources into `FlamaCorp/tbd` (the DigitalOcean data plane) would
have made credential delivery free, since `bin/run-playbook.sh` already reads
that directory. It was rejected:

* The AWS provider validates credentials at **configure** time. A configure-time
  failure fails the whole run, not just the AWS resources, so an AWS auth
  problem would block **every DigitalOcean apply** — including the one needed to
  repair the droplet.
* It would import the TBD-372 rename-lockout hazard into the workspace named
  `tbd`, where a rename breaks every DO apply rather than just backups.
* `FlamaCorp/tbd`'s state already holds `do_token` and the data-plane passwords.
  Adding the uploader key would make one state file yield the droplet **and**
  write access to the only copy of its data — the adversarial twin of the
  failure this ticket exists to fix.

## Genesis (once, by hand, with root)

The account was empty: no IAM users, no roles, no OIDC providers. Something has
to mint the first principal, and only root could.

```bash
# 0. FIRST, in the console: enable MFA on the root user.
#    AccountMFAEnabled=0 with a live root access key is a full account takeover
#    from one leaked key pair, in the account holding every customer's password
#    hash. This is a gate, not a nicety.

# 1. Create an admin break-glass IAM user (console password + MFA, NO key).
#    This is the named out-of-band hand for a TBD-372-class lockout.

# 2a. The OIDC provider.  ✅ DONE 2026-08-28 --
#     arn:aws:iam::884686184019:oidc-provider/app.terraform.io
#
#     ⚠ Do not copy a thumbprint from a runbook. Derive it, or you ship a value
#     that applies cleanly and is simply wrong -- AWS validates well-known
#     public CAs against its own trust store and ignores this list, so a bad
#     value has no symptom. The command that produced the committed value:
#
#     THUMB=$(openssl s_client -servername app.terraform.io \
#               -connect app.terraform.io:443 -showcerts </dev/null 2>/dev/null \
#             | <extract the LAST certificate> \
#             | openssl x509 -fingerprint -sha1 -noout | cut -d= -f2 \
#             | tr -d ':' | tr 'A-Z' 'a-z')
#     aws iam create-open-id-connect-provider --url https://app.terraform.io \
#       --client-id-list aws.workload.identity --thumbprint-list "$THUMB"

# 2b. BOTH roles, from the COMMITTED documents. Two, not one: the plan role is
#     what stops a speculative plan on an unapproved PR from reading backups.
aws iam create-role --role-name tfc-backups-provisioner \
  --assume-role-policy-document file://../../aws/bootstrap/tfc-backups-trust.json
aws iam put-role-policy --role-name tfc-backups-provisioner \
  --policy-name tfc-backups-provisioner-inline \
  --policy-document file://../../aws/bootstrap/tfc-backups-provisioner.json

aws iam create-role --role-name tfc-backups-plan \
  --assume-role-policy-document file://../../aws/bootstrap/tfc-backups-plan-trust.json
aws iam put-role-policy --role-name tfc-backups-plan \
  --policy-name tfc-backups-plan-read-only \
  --policy-document file://../../aws/bootstrap/tfc-backups-plan.json

# 3. CREATE the TFC workspace itself -- it does not exist yet, and nothing in
#    this repo can create it. Until it does, no speculative plan runs for this
#    directory and no `Terraform Cloud/FlamaCorp/tbd-backups` check appears on a
#    PR: its ABSENCE is not a pass.
#
#      Organization:       FlamaCorp
#      Name:               tbd-backups        (must match versions.tf exactly)
#      Type:               VCS-driven, this repo
#      Working directory:  infra/terraform/backups
#      Trigger pattern:    infra/terraform/backups/**
#      Auto-apply:         OFF  (matches tbd and tbd-apex)
#
#    Then set on that workspace:
#      TFC_AWS_PROVIDER_AUTH  = true
#      TFC_AWS_PLAN_ROLE_ARN  = arn:aws:iam::884686184019:role/tfc-backups-plan
#      TFC_AWS_APPLY_ROLE_ARN = arn:aws:iam::884686184019:role/tfc-backups-provisioner
#      aws_account_id         = 884686184019
#
#    ⚠ TWO role ARNs, not one. A single TFC_AWS_RUN_ROLE_ARN would use the same
#    role for plan and apply, which re-opens the plan-phase read this design
#    exists to close: speculative plans run on unapproved PRs.

# 4. Merge the PR, Confirm & Apply, then import the two bootstrap resources so
#    they are managed as code from here on:
terraform import aws_iam_openid_connect_provider.tfc \
  arn:aws:iam::884686184019:oidc-provider/app.terraform.io
terraform import aws_iam_role.tfc_backups_provisioner tfc-backups-provisioner
terraform import aws_iam_role.tfc_backups_plan tfc-backups-plan
terraform import aws_iam_role_policy.tfc_backups_provisioner \
  tfc-backups-provisioner:tfc-backups-provisioner-inline
terraform import aws_iam_role_policy.tfc_backups_plan \
  tfc-backups-plan:tfc-backups-plan-read-only

# 5. ONLY NOW delete the root access keys. Deleting them before a green apply is
#    the TBD-372 lockout with root as the thing locked out.
```

## Genesis status and the measured boundary

✅ **Done 2026-08-28, by CLI, from the committed documents:**

| Resource | ARN |
|---|---|
| OIDC provider | `arn:aws:iam::884686184019:oidc-provider/app.terraform.io` |
| Apply role | `arn:aws:iam::884686184019:role/tfc-backups-provisioner` |
| Plan role | `arn:aws:iam::884686184019:role/tfc-backups-plan` |

The permission boundary was then verified with `aws iam
simulate-principal-policy` rather than asserted — 28 checks, all as intended:

* the **apply** role can create the bucket, its versioning, policy, lifecycle,
  object-lock and encryption config, the uploader user and its access key, the
  probe role, and the GitHub OIDC provider;
* the **plan** role can read all of that state and can write **none** of it;
* **neither** role can read backup content: `s3:GetObject`,
  `s3:GetObjectVersion`, `kms:Decrypt` and `kms:ReEncryptFrom` all evaluate to
  `explicitDeny`;
* neither can touch a bucket or an IAM principal outside this feature
  (`implicitDeny`).

⚠ Re-run that simulation after any edit to either policy. The scoping is tight
enough that a genuine apply failure is plausible; the fix is a policy update,
and it is far preferable to the account-admin role this replaced.

## The self-authorization hazard, and why it is survivable here

This workspace manages the trust policy that admits this workspace. That is the
TBD-372 shape, and it is unavoidable inside one account touched by one
workspace. Three defences:

1. `backend/tests/test_backup_offhost.py` asserts at **PR time** that the
   workspace named in `versions.tf` equals the workspace in the committed trust
   document. TBD-372 happened because a rename was *applied* with the pattern
   unchanged; this stops that from being applied at all.
2. `prevent_destroy` on the role and the OIDC provider.
3. A plan-time `precondition` on the role, so an apply from a branch that
   skipped review still fails rather than locking the workspace out.

To rename the workspace: **widen** the trust pattern to span both names, apply,
rename, then narrow. Never rename first.

## What the droplet can and cannot do

`pfv-backup-uploader` holds `s3:PutObject` on one prefix plus
`kms:GenerateDataKey`/`Encrypt`/`DescribeKey` on one key. It has **no**
`GetObject`, no `ListBucket`, no `DeleteObject`, and an explicit **`Deny` on
`kms:Decrypt`** in the key policy. An explicit key-policy Deny is not
overridable by an IAM policy or a bucket policy, so the droplet writes
ciphertext it cannot read. A stolen key is a nuisance, not a breach.

⚠ One honest bound on that claim: `bucket_key_enabled = true` means S3 caches a
bucket-level data key rather than calling KMS on every object read, so the key
policy is not necessarily evaluated on every individual GET within the cache
window. No principal here holds `s3:GetObject` at all, so there is no live path
today — but do not lean on the KMS Deny as an absolute second gate without
re-checking that interaction. The IAM and bucket-policy Denies are the ones that
hold unconditionally.

⚠ The TFC roles are split by run phase because HCP Terraform runs a speculative
plan on every PR, before approval. A single `run_phase:*` role with broad
permissions would let an unmerged PR read production backups. Both roles also
carry an explicit Deny on `s3:GetObject` and `kms:Decrypt`: Terraform manages
the bucket, and never needs a backup's contents.

Object Lock is GOVERNANCE mode: a compromised droplet cannot overwrite history,
but break-glass can still clean up a mistake.
