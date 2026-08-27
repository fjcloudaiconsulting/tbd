# Off-host MySQL backup to S3 (TBD-400)

**Status:** design settled 2026-08-27 by two independent architects, crossed twice.
**Depends on:** TBD-416 (merged `df7151cd`, **not yet converged on production**).

## The problem

`roles/backups/templates/mysql-backup.sh.j2` writes to `/var/backups/mysql` on the
droplet it is protecting. Droplet backups are off (TBD-399), so a disk or droplet
loss takes the production data and its only backup together.

Measured on the box 2026-08-27: dumps are ~620 KB gzipped, growing ~4 KB/day, 10
files retained, 4.6 GB used of 24 GB. Steady-state bucket size is ~5 MB, so
storage cost rounds to zero and the S3-over-Spaces ruling is more lopsided than
the ticket's 217 GB break-even implied.

## Four holes, and which are in scope

| # | Hole | Status |
|---|---|---|
| 1 | No off-host copy | **this spec** |
| 2 | Grants/users not dumped, so a restore yields tables and zero logins | **this spec** |
| 3 | A failed run leaves a plausible artifact | half-closed by TBD-416 (`.part` + `mv`); the *content* checks are **this spec** |
| 4 | No failure alerting | **this spec** |

## Architecture

```
droplet (cron 02:00 UTC)
  mysqldump  -> pfv2-<ts>.sql.gz.part
  grants     -> grants-<ts>.sql.gz.part
  mysql-backup-verify.sh   <- gate: gzip -t, completion marker, live table count, pfv_app present
  mv both into place                       (local copy = fast restore path)
  s3api put-object x3, manifest.json LAST  (manifest present => that night completed)

GitHub Actions (cron 04:17 UTC, OIDC, ListBucket only)
  check-backup-freshness.sh  -> stale/missing/undersized -> notify-backup-stale.sh -> GitHub issue
```

## Rulings

### R1. A separate TFC workspace `FlamaCorp/tbd-backups` at `infra/terraform/backups/`

Not the apex workspace (different AWS account; ticket forbids it). **Not** the DO
workspace either, though that was initially argued for because
`run-playbook.sh` already reads it:

⚠ The AWS provider validates credentials at **configure** time
(`sts:GetCallerIdentity`). A configure-time failure fails the whole run, not just
the AWS resources, and `-target` does not rescue it. So a two-provider
`FlamaCorp/tbd` would make AWS auth a hard dependency of the repair path for the
droplet, VPC and firewall at `infra/terraform/main.tf:58-131` — you could not
apply a DigitalOcean fix until an AWS trust policy was repaired out of band.

⚠ It would also import the TBD-372 rename hazard into the workspace named `tbd`,
where a rename would break **every** DO apply rather than just backups.

⚠ Secondary: `FlamaCorp/tbd`'s state already holds `do_token` and the three
`random_password` values. Adding the uploader secret would make one state file
yield the droplet **and** write access to the only copy of its data — the
adversarial twin of the failure this ticket exists to fix.

Cost accepted: `run-playbook.sh` reads a second `-chdir`, and there are two
bootstraps to keep straight.

### R2. The trust anchor is committed, Terraform-managed, and drift-checked

The operator's rule is "we don't want infra without code", so a permanently
hand-applied IAM role is out. Genesis is unavoidable in an empty account (root is
the only principal), so: root creates the OIDC provider and provisioner role
once from the **committed** JSON in `infra/aws/bootstrap/`, then
`terraform import` brings both under management.

Three defences against the TBD-372 self-authorization class:

1. **A PR-time fence** asserting the workspace literal in `backups/versions.tf`'s
   `cloud { workspaces { name } }` equals the workspace named in the trust
   policy's `app.terraform.io:sub` condition. ⚠ This is the one that would have
   *prevented* TBD-372, which happened because a rename was applied with the
   pattern unchanged. Non-management would only have made recovery easier.
2. `lifecycle { prevent_destroy = true }` on the role and the OIDC provider.
3. Plan-time drift: a postcondition comparing the live trust policy to the
   committed JSON.

Trust pins the **exact** workspace name (`StringEquals`), not apex's `tbd-apex*`
glob — that wildcard is scar tissue from the rename and a fresh anchor should not
inherit it. The widen -> rename -> narrow procedure from `apex/variables.tf:49-51`
goes in the new variable's comment verbatim.

### R3. The droplet gets a put-only credential, delivered through TFC state

⚠ **Not ansible-vault.** `run-playbook.sh:12-14` bans a committed vault file
because this repository is PUBLIC, making it "a permanent harvestable artefact
regardless of passphrase strength". The ticket's instruction to use the vault
contradicts the repo's own standard and is overruled.

The secret comes to rest in exactly two places: TFC state (same boundary as
`mysql_app_password`) and mode-0600 on the droplet. Never the repo, never
inventory, never argv.

⚠ The templating task carries `no_log: true`, and that is not optional:
`--check --diff` renders template content, which is how these credentials became
quotable in a transcript before (TBD-414).

### R4. Verification, resolving the DoD's contradiction

The DoD demands verification "against the uploaded object" **and** a credential
with no read. Those are incompatible for one principal at one moment. Split it:

| Layer | Proves | Where | Needs |
|---|---|---|---|
| A content | the dump is complete and covers the schema | droplet, pre-upload, on the `.part` | MySQL SELECT |
| B transport | the bytes S3 stored are the bytes sent | S3, at PUT | nothing extra |
| C presence | the object exists, is recent, is plausibly sized | GitHub Actions | `s3:ListBucket` |
| D restorability | the dump actually restores | quarterly manual drill | a separate MFA-gated role |

**Layer B is the piece the ticket assumed did not exist:** `put-object
--checksum-algorithm SHA256` makes S3 recompute the digest server-side and reject
a mismatch with `BadDigest`. The PUT's own exit status *is* verification of the
stored object, with zero read permission.

⚠ **Layer D cannot be automated and must not be faked.** Nothing in scope proves
a stored byte is readable; a misconfigured KMS grant would pass A-C nightly for
months. The quarterly drill is therefore a deliverable, not a footnote.

⚠ **Do not hardcode the table count.** 50 is a measurement of today's schema, not
an invariant; a literal turns the next migration into a red check against a
correct backup. Read it live from `information_schema` and pass it in.

⚠ **Do not stream `mysqldump | gzip | aws s3 cp -`.** It destroys the local
fast-restore copy and forces a multipart/unsigned body that loses the
whole-object checksum. Upload the finished file after the `mv`.

### R5. Alerting detects SILENCE, from off the box

⚠ An alert emitted by the droplet cannot fire when the droplet is gone or cron
never ran, which is exactly the disaster. The detector must live elsewhere.

A scheduled GitHub Actions probe (`04:17 UTC`, ~2.3 h after the dump) reads only
S3 metadata via OIDC and opens a deduped `[backup-stale]` issue. Thresholds:
object age > **25 h** (alarms on a single miss, ~23 h of slack for runner
queueing), size below a floor, and the **manifest** present — not merely "some
object exists", which reads a bucket of week-old dumps as healthy forever.

⚠ On "could not run" (exit 2) the probe opens an issue too, rather than only
failing the job. A red scheduled workflow notifies nobody, and nothing else
covers this signal.

⚠ Separate dedupe bucket from the smoke/drift alarms, per
`notify-undeployed-release.sh`'s stated reasoning: collapsing buckets lets one
alarm silence another.

### R6. Grants as a second artifact; no privilege widening

`roles/mysql/tasks/main.yml:173` already grants `pfv_backup` global `SELECT`,
which covers `mysql.user`. Emit `SHOW CREATE USER` + `SHOW GRANTS` per account.
Not `mysqldump mysql`: restoring that schema wholesale across builds is
unsupported.

⚠ The bucket was maximum-sensitivity from object one — the `pfv2` dump already
holds every bcrypt hash, MFA secret and PAT hash. What grants add is different in
kind: the **live `pfv_app` credential**, offline-crackable, for a server whose
3306 is reachable from the VPC. Hence:

* **SSE-KMS with a customer-managed key.** With AES256, `s3:GetObject` *is*
  plaintext. With a CMK, reading additionally requires `kms:Decrypt` on a surface
  no bucket policy can grant, and `DisableKey` is one-lever revocation.
* ⚠ **Keep root delegation in the key policy** (omitting it is the classic KMS
  lockout footgun, especially in an account whose root keys we then delete), and
  get the independence via an explicit **`Deny` on `kms:Decrypt`** for the
  uploader and probe ARNs. An explicit key-policy Deny cannot be overridden by
  any IAM or bucket policy.
* The uploader gets `kms:GenerateDataKey*`/`Encrypt`/`DescribeKey`, never
  `Decrypt`: it writes ciphertext it cannot read.
* **Object Lock, GOVERNANCE.** The credential lives on the box being defended, so
  a compromised droplet must not be able to overwrite history. GOVERNANCE, not
  COMPLIANCE, so break-glass can clean up a mistake. Free at creation, painful to
  retrofit.
* ⚠ **With versioning on, a bare `expiration` rule writes delete markers and
  deletes nothing.** The rule set needs `noncurrent_version_expiration` and
  `expired_object_delete_marker` or retention is fiction.
* **STANDARD storage class only.** IA/Glacier carry 30/90-day minimum billing
  durations; at 7-day retention they cost *more*.

⚠ The identity policy conditions on the encryption header, so the script must
send `--server-side-encryption aws:kms --ssekms-key-id <arn>` explicitly. A
`StringEquals` on an **absent** header fails, and relying on the bucket default
to satisfy it does not work — the resulting 403 reads like a credential problem.

### R7. No ansible from CI

The firewall is **not** the blocker (`modules/firewall/main.tf:9-13` opens SSH to
`0.0.0.0/0`; ufw is explicitly disabled). The real blockers:

1. Every `--production` run **rotates** the data-plane credentials
   (`roles/mysql/tasks/main.yml:117-139`), and the app authenticates with the old
   password until both `DATABASE_URL` bindings are re-encrypted and redeployed.
2. Converging **restarts MySQL** on the single node holding all user data.
3. An SSH key with a shell on that box, in a GitHub secret, gives root on the
   data plane to anything that can run a workflow. `apex/main.tf:878-885` already
   rejects workflow-level guards for a strictly smaller prize.

⚠ The obvious middle ground is also unsafe: a scheduled `--check --diff` drift
probe would print `root.my.cnf.j2`'s cleartext password (TBD-414).

**What is automated instead:** the freshness probe (needs no SSH at all), TFC's
existing speculative plan on PR, and a **`backups` tag** so this role can be
converged alone. That tag is a prerequisite, not a nicety: `run-playbook.sh:35-41`
records this tree has no topic tags, so today the only way to deploy this feature
is a full converge, which rotates credentials.

## Rollout order

⚠ Ordering is a ruling, not a preference.

1. Operator, once: **enable root MFA** (`AccountMFAEnabled=0` with a live root
   key is the account's actual emergency), create `tbd-break-glass`, then create
   the OIDC provider + provisioner role from `infra/aws/bootstrap/`.
2. Merge the PR. TFC speculative plan on PR; **Confirm & Apply** on merge.
3. `terraform import` the two bootstrap resources.
4. Refresh the expired TFC token (currently HTTP 401), then converge **once**:
   `bin/run-playbook.sh --production -- --tags backups`.
   ⚠ **Do not converge TBD-416 separately.** Every converge rotates credentials
   and restarts MySQL, so land both and pay one window, not two.
5. Delete the root access keys — **after** a green apply, never before.

⚠ Between merge and converge the probe will open a `[backup-stale]` issue. That
alarm is **true**: there is no off-host backup yet. Do not add a grace period;
that is a snooze button on the alarm's first day.

⚠ Production is still unconverged, so it still streams to the final name. A file
at the final name today can be a truncated dump that `gzip -t` passes. The upload
must therefore be gated on the freshly produced `.part`, and there must be no
"upload the newest file in the backup dir" convenience path — that would
faithfully replicate a corrupt artifact off-host and mark it verified.

## Fences

Every one is `fence` (kills a named wrong implementation), not `guard`.

| Fence | Kills |
|---|---|
| execute `mysql-backup-verify.sh` on fabricated artifacts | a verification that only ever runs against a healthy dump |
| `json.load` the put-only policy, exact action set both directions | a future `s3:GetObject` widening |
| parse the backup script: `put-object` not `s3 cp`, checksum flag, SSE flags, verify precedes upload, manifest last | streaming, unchecked PUT, unverified upload |
| workspace literal in `cloud{}` == workspace in trust JSON `sub` | the TBD-372 rename lockout |
| execute `check-backup-freshness.sh` against fixture listings | a probe that reads a stale bucket as healthy |
| `no_log` on the credential task | TBD-414 recurrence |
