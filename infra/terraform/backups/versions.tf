terraform {
  required_version = ">= 1.6"

  # ⚠⚠ THIS IS A DIFFERENT AWS ACCOUNT FROM infra/terraform/apex/.
  # apex (the public landing site) lives in the operator's older account; this
  # workspace targets the company account 884686184019, which was empty at
  # genesis. Do not copy an account id between the two.
  #
  # State + plan/apply run in Terraform Cloud (FlamaCorp/tbd-backups),
  # VCS-driven against this repo, working directory infra/terraform/backups/,
  # trigger pattern infra/terraform/backups/**. Speculative plans on PR; merges
  # to main create runs awaiting manual Confirm & Apply. Auto-apply is off,
  # matching FlamaCorp/tbd and FlamaCorp/tbd-apex.
  #
  # ⚠ WHY A THIRD WORKSPACE, AND NOT THE DATA-PLANE ONE (TBD-400).
  # Folding these AWS resources into FlamaCorp/tbd was argued for -- it would
  # make the credential delivery free, since bin/run-playbook.sh already reads
  # that one directory. It was rejected:
  #
  #   The AWS provider validates credentials at CONFIGURE time
  #   (sts:GetCallerIdentity). A configure-time failure fails the whole run, not
  #   merely the AWS resources, and -target does not rescue it. So a
  #   two-provider FlamaCorp/tbd would make AWS auth a hard dependency of the
  #   REPAIR PATH for the droplet, VPC and firewall. You could not apply a
  #   DigitalOcean fix until an AWS trust policy was repaired out of band.
  #
  #   It would also import the TBD-372 rename-lockout hazard into the workspace
  #   named `tbd`, where a rename would break EVERY DO apply, not just backups.
  #
  #   And FlamaCorp/tbd's state already holds do_token plus the three
  #   random_password values; adding the uploader secret would make ONE state
  #   file yield the droplet AND write access to the only copy of its data --
  #   the adversarial twin of the failure this whole ticket exists to fix.
  #
  # ⚠ RENAMING THIS WORKSPACE IS NOT JUST AN EDIT HERE. The name is also the
  # AWS trust boundary: it appears in the `app.terraform.io:sub` condition of
  # infra/aws/bootstrap/tfc-backups-trust.json, which is managed BY this
  # workspace. Renaming without widening first denies
  # AssumeRoleWithWebIdentity, and the workspace then cannot apply its own fix
  # (that is TBD-372, which cost an out-of-band trust-policy edit on
  # 2026-08-11). Procedure: widen the pattern to span both names, apply, rename,
  # then narrow. NEVER rename first.
  # backend/tests/test_backup_trust_anchor.py fences the two against each other
  # at PR time, which is what prevents the event rather than easing recovery.
  cloud {
    organization = "FlamaCorp"
    workspaces {
      name = "tbd-backups"
    }
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.50"
    }
  }
}
