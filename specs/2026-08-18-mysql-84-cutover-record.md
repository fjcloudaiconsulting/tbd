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
| Start (UTC) | 15:13 — env-var save, which is when the first deploy fired |
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
| Bare-`mysql` identity, before → after Phase 1 | `pfv_backup@localhost` → `root@localhost`. The `/root/.my.cnf` footgun (PR #675) confirmed removed **on the box**, not merely in the template |

## Timings — the point of this file

Predicted 49s for 3.1 → 3.2 (measured on a rehearsal droplet, fresh Ubuntu,
same size/region, empty schema). Record the real numbers so the next estimate is
calibrated rather than asserted.

| Phase | Predicted | Actual |
|---|---|---|
| 1 — play + secret re-encrypt | ~5 min | **~12 min** — inflated by an ordering mistake, see Deviations |
| 2 — scale to 0 + cold snapshot | 5–10 min | **2 min 27 s** exactly: power-off 7 s (15:28:22→29), snapshot **58 s** (15:29:00→15:29:58), power-on 18 s (15:30:31→49). Scaling to 0 was **impossible**, see Deviations |
| 3 — slow shutdown → 8.4 answering | **49 s** | **~3.5 min** (~15:31:30 → 15:35:00). The 49 s rehearsal figure measured shutdown→start only; package download and the DD upgrade are the rest |
| 4 — verification | ~5 min | ~5 min, plus ~7 min on an unrelated Redis fault |
| 5 — rename | < 1 min | **NOT RUN** — deliberately deferred |

## Config, before and after

Paste the Phase 4.1 output verbatim.

```
expected: 0.0.0.0 | utf8mb4_0900_ai_ci | 805306368 | 1000/2000 | redo=268435456
actual:   0.0.0.0 | utf8mb4_0900_ai_ci | 805306368 | 1000/2000 | redo=268435456
```

⚠ `innodb_doublewrite_pages` should read **128**, not 4 — deliberate, not pinned.

## The three open questions production answers

The container and scratch-droplet rehearsals both passed these. Production is
the third and only authoritative data point.

| Question | Rehearsal | Production |
|---|---|---|
| Does Oracle's packaging still read `/etc/mysql/mysql.conf.d`? | Yes, all 6 vars held | **Yes** — all 6 exact, despite the install logging `update-alternatives: using /etc/mysql/mysql.cnf to provide /etc/mysql/my.cnf`, which is precisely the event that could have dropped the include |
| Does `/etc/mysql/debian.cnf` survive? (logrotate authenticates with it) | Yes | **Yes** — present after the swap |
| Does the `debian-sys-maint` account survive? | Yes | (see Phase 6 check) |

⚠ If `debian.cnf` is gone, the slow query log silently stops rotating and fills
the disk **days later**. Not urgent, not ignorable — file it.

## Deviations from the runbook

**1. Phase 2.1 (scale the backend to 0) is IMPOSSIBLE on this app.** The
`backend` component is on the legacy `basic-xxs` $5 plan, which the console
pins to exactly one container ("This plan is limited to 1 container. Plans
starting at $12.00/mo can manually scale or autoscale"). The runbook's headline
quiesce step cannot be performed as written, and nobody had tried it before the
window.

What replaced it: **Phase 1 quiesces the app for free.** Rotating the
credentials locks the backend out of both MySQL and Redis, so it cannot write.
That is not a workaround bolted on — it is a property of Phase 1 that the sheet
never noticed it had.

**2. The env-var re-encrypt (1.2) was done BEFORE the play (1), not after.**
That inverts the sheet and cost ~7 minutes. The save fired a deploy whose
`migrate` PRE_DEPLOY job authenticated with the *new* password against a server
that still had the *old* one; it failed at 6/12, App Platform kept the previous
deployment alive, and a second deploy was needed after the play. Harmless, but
the ordering in the sheet is load-bearing and should be stated as such.

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
