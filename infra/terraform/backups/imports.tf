# Genesis reconciliation (TBD-400).
#
# The OIDC provider and both roles were created BY HAND with root on 2026-08-28,
# from the committed documents in infra/aws/bootstrap/. That step is unavoidable
# in a greenfield account: a workspace cannot assume a role that does not exist,
# and root was the only principal able to mint the first one.
#
# ⚠ WITHOUT THESE BLOCKS THE FIRST APPLY FAILS. Terraform would try to CREATE
# resources that already exist and stop on EntityAlreadyExists.
#
# ⚠ `import` BLOCKS RATHER THAN `terraform import` COMMANDS, deliberately. This
# workspace is VCS-driven with remote execution, so there is no local CLI run to
# hang a state operation off; the import has to be part of the plan the operator
# confirms. It is also reviewable: the plan shows exactly what is being adopted.
#
# ⚠ REMOVE THIS FILE once the first apply has succeeded. Leaving it is harmless
# (an import block for an already-managed resource is a no-op) but it is dead
# weight, and the next reader should not have to work out whether genesis is
# still pending.
import {
  to = aws_iam_openid_connect_provider.tfc
  id = "arn:aws:iam::884686184019:oidc-provider/app.terraform.io"
}

import {
  to = aws_iam_role.tfc_backups_provisioner
  id = "tfc-backups-provisioner"
}

import {
  to = aws_iam_role.tfc_backups_plan
  id = "tfc-backups-plan"
}

import {
  to = aws_iam_role_policy.tfc_backups_provisioner
  id = "tfc-backups-provisioner:tfc-backups-provisioner-inline"
}

import {
  to = aws_iam_role_policy.tfc_backups_plan
  id = "tfc-backups-plan:tfc-backups-plan-read-only"
}
