# TBD-360 — cutover record

**Status: EXECUTED 2026-08-19. Production is on MySQL 8.4.11.**

Timings below are reconstructed from App Platform log timestamps, `doctl` action
records, and the order of operations in the session transcript. The `doctl`
rows are exact to the second; the rows marked ~ are bounded by log entries
either side and are accurate to roughly a minute.

---

## Header

| | |
|---|---|
| Date / operator | 2026-08-19, flamarion (agent-driven, operator ran every SSH and console step) |
| Start (UTC) | 15:13 — env-var save, which is when the first deploy fired. ⚠ User impact did not begin until ~15:18, when the play rotated the credentials out from under the running container; 15:13–15:18 is the failed deploy, during which the app served normally |
| End (UTC) | 15:42 — Google SSO login confirmed working |
| **Total user-visible outage** | **~24 min**, of which ~8 min database-down and a further ~7 min with the DB up but logins failing on an unrelated Redis fault |
| Predicted | ~15–25 min total, ~1 min of it database |
| Rollback used? | **No.** Snapshot never invoked. |

## Pre-flight

| Check | Value |
|---|---|
| Backup ID relied on | `241738206` (2026-08-19 00:21 UTC) |
| Snapshot ID taken (Phase 2) | `241824729` — `tbd360-pre-84-20260819-1729`. ⚠ Restore verb is **`rebuild`**, not `restore` |
| MySQL version before | `8.0.46-0ubuntu0.24.04.3` |
| Dataset before | 50 tables / 6.7 MB |
| `--check` dry run clean? (`changed=` count, and note that the play's fences are skipped under `--check`) | Yes — `ok=40 changed=9 failed=0`, run twice (18th and 19th) with an identical nine. The real Phase 1 run was `ok=46 changed=10 failed=0`; `skipped` fell 10 → 4, which is the six check-mode-gated verification tasks executing |

⚠ **The tenth changed task is UNIDENTIFIED.** The dry run's nine were timezone,
three user tasks, `/root/.my.cnf`, `pfv.cnf`, the redis drop-in and two restart
handlers. The real run reported ten, and the newly-executing gated tasks cannot
account for it: five are `changed_when: false`, `set_fact` or `assert`, and the
sixth — the corrective restart — must have skipped, or `skipped` would not be
4. The Phase 1 log was deleted (it carried the rotated credentials in its
diff), so this cannot now be recovered.

**The candidate that would matter is `Update apt cache and upgrade packages`.**
If that was it, an apt run happened inside the window — the exact thing Phase
0.4 exists to move out of it — and it would help explain Phase 1 taking 12 min
against a predicted 5. ⚠ On the next window, **capture the changed-task names
from the real run**, not just the count. Do not treat this as settled.
| Bare-`mysql` identity, before → after Phase 1 | `pfv_backup@localhost` → `root@localhost`. The `/root/.my.cnf` footgun (PR #675) confirmed removed **on the box**, not merely in the template |

## Timings — the point of this file

Predicted 49s for 3.1 → 3.2 (measured on a rehearsal droplet, fresh Ubuntu,
same size/region, empty schema). Record the real numbers so the next estimate is
calibrated rather than asserted.

| Phase | Predicted | Actual |
|---|---|---|
| 1 — play + secret re-encrypt | ~5 min | **~12 min** — inflated by an ordering mistake, see Deviations |
| 2 — scale to 0 + cold snapshot | 5–10 min | **2 min 27 s** exactly: power-off 7 s (15:28:22→29), snapshot **58 s** (15:29:00→15:29:58), power-on 18 s (15:30:31→49). Scaling to 0 was **impossible**, see Deviations |
| 3 — slow shutdown → 8.4 answering | **49 s** | **~3.5 min** (~15:31:30 → 15:35:00). ⚠ See the note below — the rehearsal figure was **not** mis-scoped |
| 4 — verification | ~5 min | **~7 min total** (15:35 → 15:42), nearly all of it the unrelated Redis fault. The verification proper was minutes; the two overlap and do not sum |
| 5 — rename | < 1 min | **NOT RUN** — deliberately deferred |

⚠ **Why phase 3 took ~3.5 min against a measured 49 s, and what it is NOT.**
The 49 s was not a narrower span: `rehearse-84-scratch-droplet.sh` starts its
timer at `PACKAGE SWAP + DD UPGRADE — this is the measured window` and stops at
`first authenticated query on 8.4`, i.e. the same shutdown→answering leg, full
package download included, on a real droplet.

The difference is that the rehearsal was **one scripted run with no human in
it**, while production was executed as four separate operator-typed `$SSHQ`
commands with turnaround between each (see Deviation 5 — every SSH step was
operator-run), and 3.1 was run twice. The engine work was the same; the
wall-clock is dominated by the hand-off.

**For the next window: budget from the operator's cadence, not from the
rehearsal.** A scripted leg and a copy/paste leg are different measurements of
different things, and this sheet is copy/paste by design.

## Config, before and after

Paste the Phase 4.1 output verbatim.

```
expected: 0.0.0.0 | utf8mb4_0900_ai_ci | 805306368 | 1000/2000 | redo=268435456
actual:   0.0.0.0 | utf8mb4_0900_ai_ci | 805306368 | 1000/2000 | redo=268435456
```

⚠ `innodb_doublewrite_pages` should read **128**, not 4 — deliberate, not pinned.
Production reads `doublewrite_pages=128`, as expected.

## Phase 6 — close-out evidence

```
debian-sys-maint | localhost | caching_sha2_password
doublewrite_pages=128
pfv2_20260818-020001.sql.gz   586K   (nightly, pre-cutover, 8.0)
pfv2_20260819-020001.sql.gz   593K   (nightly, pre-cutover, 8.0)
pfv2_20260819-155940.sql.gz   598K   (by hand, post-cutover, 8.4)
```

The hand-run dump is the one that matters: it proves `mysqldump` still works
against 8.4, which nothing else in this window exercised.

Verified as a **complete** dump, not merely a file that exists — which is the
whole point, because the script's known hole is that `gzip >` creates the file
before `mysqldump` can fail:

```
zcat pfv2_20260819-155940.sql.gz | tail -2   -> -- Dump completed on 2026-08-19 15:59:41
zcat pfv2_20260819-155940.sql.gz | grep -c '^CREATE TABLE'  -> 50
```

50 tables matches the schema, and the completion marker is the last line. A
truncated dump ends mid-statement with no marker. **This is the check the
nightly cron does not do** — TBD-400 should make the script assert both and
fail loudly, rather than leaving it to whoever thinks to look.

⚠ **The nightly backup is now the ONLY backup.** TBD-399 (revert droplet
backups to disk-only) is deliberately **held, blocked on TBD-400** — reverting
it would leave production with no off-host copy at all, because this script
writes to `/var/backups/mysql/` on the same droplet. Do not merge TBD-399 until
TBD-400 delivers a verified off-host destination.

## The three open questions production answers

The container and scratch-droplet rehearsals both passed these. Production is
the third and only authoritative data point.

| Question | Rehearsal | Production |
|---|---|---|
| Does Oracle's packaging still read `/etc/mysql/mysql.conf.d`? | Yes, all 6 vars held | **Yes** — all 6 exact, despite the install logging `update-alternatives: using /etc/mysql/mysql.cnf to provide /etc/mysql/my.cnf`, which is precisely the event that could have dropped the include |
| Does `/etc/mysql/debian.cnf` survive? (logrotate authenticates with it) | Yes | **Yes** — present after the swap |
| Does the `debian-sys-maint` account survive? | Yes | **Yes** — `debian-sys-maint@localhost`, on `caching_sha2_password` |

⚠ If `debian.cnf` is gone, the slow query log silently stops rotating and fills
the disk **days later**. Not urgent, not ignorable — file it.

## Deviations from the runbook

**1. Phase 2.1 (scale the backend to 0) is IMPOSSIBLE on this app.** The
`backend` component is on the legacy `basic-xxs` $5 plan, which the console
pins to exactly one container ("This plan is limited to 1 container. Plans
starting at $12.00/mo can manually scale or autoscale"). The runbook's headline
quiesce step cannot be performed as written, and nobody had tried it before the
window.

⚠ **And nothing replaced it. Nothing quiesced the app.** An earlier draft of
this record claimed Phase 1 quiesces it "for free" by locking the backend out
of its own credentials. That is true only in the window created by Deviation 2
below — the *mistake* of updating the env vars before running the play. On the
order this sheet now mandates, 1.2 restores working credentials on purpose, and
the app serves writes normally until MySQL stops in 2.2.

**The real exposure: writes taken between 1.2 and the 2.2 shutdown are lost if
the snapshot is ever used.** Measured here at roughly two minutes, and judged
negligible for this app's traffic. It was an accepted risk, not an eliminated
one, and the runbook now says so.

**2. The env-var re-encrypt (1.2) was done BEFORE the play (1), not after.**
That inverts the sheet and cost ~7 minutes. The save fired a deploy whose
`migrate` PRE_DEPLOY job authenticated with the *new* password against a server
that still had the *old* one; it failed at 6/12, App Platform kept the previous
deployment alive, and a second deploy was needed after the play. Recoverable,
but not harmless — it accounts for roughly seven of the twenty-four minutes —
and the ordering in the sheet is load-bearing rather than stylistic. It now
says so.

**3. A clean slow shutdown was added BEFORE the snapshot** (not in the sheet,
which powers off a running server). `SET GLOBAL innodb_fast_shutdown = 0` +
`mysqladmin shutdown`, then power-off. The snapshot is therefore an image of a
cleanly-closed InnoDB rather than a crash-consistent one — a strictly better
rollback target, at the cost of one extra command. 3.1 was then repeated after
power-on, because MySQL auto-starts on boot.

**4. The install command was changed to preserve its exit status.** The sheet
piped `apt-get install` to `tail -15`, which reports the pipeline's status
(`tail`'s, always 0). Replaced with a redirect to `/tmp/tbd360-install.log`,
`echo "install-exit=$?"`, then `tail` of the file.

**5. Division of labour.** The agent driving this could run `ansible` and
read-only `doctl`, but the classifier blocked `ssh`, ad-hoc `ansible -m
command`, `doctl apps update`, `doctl apps create-deployment` and firewall
edits. Every SSH and console step was operator-run. Plan for this: it is not a
transient condition.

## The Redis incident — unrelated to 8.4, exposed by the rotation

After the cutover, `/ready` was green and `/api/v1/auth/status` returned 200,
but **Google SSO login returned 503**. `auth.py` fails *closed* on an
unreachable Redis (it is the session store), which is correct behaviour and is
what surfaced it.

Root cause: `/etc/redis/redis.conf` carried two directives **after** the
`include /etc/redis/conf.d/*.conf` line at 2276 —

```
requirepass "<old password>"
user default on #<sha256 of old password> ~* &* +@all
```

Redis parses top-to-bottom and **last directive wins**, so both shadowed the
drop-in the role manages. They were written by `CONFIG REWRITE` in the role's
`Apply redis live tunables` handler on some earlier run, persisting the
then-running password into the main config file.

⚠ **This was latent for as long as that handler has existed and was invisible
because the shadowed value and the intended value were the same string.**
Rotating the password is what made them disagree. Any restart of that box would
have exposed it eventually; the migration merely got there first.

⚠ **`requirepass` alone was not the fix.** In Redis 6+ `requirepass` is sugar
for the default user's password, and an explicit `user default on #<hash>` line
overrides it outright. Deleting only the line we first noticed would have left
the old password in force via the ACL entry.

Fixed in place: both lines deleted, `redis-server` restarted, `PONG` confirmed
against the drop-in password, `appendonly yes` confirmed so the AOF replayed and
sessions survived. `/etc/redis/redis.conf` backed up to `/root/redis.conf.bak-*`
first. **The role itself is still wrong** — see follow-ups.

⚠ **Only the two lines that were shadowing a credential were removed by hand.**
`CONFIG REWRITE` also left `maxmemory`, `save`, `supervised` and
`latency-tracking-info-percentiles` after the include. `maxmemory` is
role-owned, and it happens to equal the drop-in's value today — which is
exactly how the requirepass shadowing stayed invisible for months. TBD-412's
purge removes it. ⚠ **Run that play before TBD-414's rotation**, and expect the
purge to report `changed`; if it reports `ok`, the regex did not match what is
on the box and that needs looking at.

## Follow-ups raised

| | Ticket |
|---|---|
| Revert `enable_backups` to `false` | TBD-399 |
| Backup hardening (off-host, grants, alerting) | TBD-400 |
| SSH open to `0.0.0.0/0` | TBD-370 |
| Ansible CI + DO dynamic inventory | TBD-206 |
| Remaining `pfv*` names (users, config file, droplet) | TBD-205 / TBD-396 |
| **Redis role: `CONFIG REWRITE` shadows the drop-ins** — role fix plus a converge-then-assert fence | **TBD-412** (PR #679) |
| **Runbook: Phase 2.1 cannot be executed** on the current plan | Fixed in this PR |
| **`/ready` does not check Redis** — green while nobody could log in | **TBD-413** |
| **Rotate `mysql_app_password` and `redis_password`** — exposed in a transcript during the window. ⚠ Do this AFTER TBD-412 | **TBD-414** |
| Phase 5 rename `pfv2` → `tbd` — deliberately deferred, trivially reversible, do it as its own operation | TBD-360 follow-up |
| Delete snapshot `241824729` once confident (holds a full copy of production; keep a few days) | — |
| ~~**Drop the `mysql: ["8.0","8.4"]` CI matrix leg**~~ — DONE 2026-08-20 (the `test.yml` comment that asked for it was removed in the same change). No environment runs 8.0 now. Safe for branch protection: only the two aggregate contexts are required, and `needs:` resolves over whatever legs exist | **TBD-415** |
| ~~**Clear the remaining "production is 8.0" notes**~~ — DONE 2026-08-20. ⚠ The list that followed was INCOMPLETE: it omitted `infra/ansible/bin/run-playbook.sh`, which carried the identical scale-to-0 sentence as `infra/ansible/README.md`, plus further scale-to-0 and window claims inside `MYSQL-84-CUTOVER.md`. The real count was ~11 sites across 8 files, not 5. — `docker-compose.prod.yml`, `MYSQL-84-CUTOVER.md`, `MIGRATION.md`, `infra/ansible/README.md`, and add production as the final evidence row in `MYSQL-84-CUTOVER.md` | **TBD-415** |
| ⚠ **Phase 5's rename needs a quiesce that does not exist.** `RENAME TABLE` takes metadata locks, so unlike Phase 2 the quiesce there is load-bearing — and scaling to zero is unavailable. Decide firewall rules vs. a plan bump *before* attempting it | **TBD-416** |
