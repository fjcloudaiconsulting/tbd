# TBD-360 — cutover record

**Status: NOT YET EXECUTED.** This is the form to fill in *during* the window,
not afterwards from memory. Every field exists because someone later will need
it and will not be able to reconstruct it.

Fill it in as you go, commit it when done.

---

## Header

| | |
|---|---|
| Date / operator | |
| Start (UTC) | |
| End (UTC) | |
| **Total user-visible outage** | |
| Predicted | ~15–25 min total, ~1 min of it database |
| Rollback used? | |

## Pre-flight

| Check | Value |
|---|---|
| Backup ID relied on | |
| Snapshot ID taken (Phase 2) | |
| MySQL version before | `8.0.46-0ubuntu0.24.04.3` |
| Dataset before | 50 tables / 6.7 MB |
| `--check` dry run clean? | |

## Timings — the point of this file

Predicted 49s for 3.1 → 3.2 (measured on a rehearsal droplet, fresh Ubuntu,
same size/region, empty schema). Record the real numbers so the next estimate is
calibrated rather than asserted.

| Phase | Predicted | Actual |
|---|---|---|
| 1 — play + secret re-encrypt | ~5 min | |
| 2 — scale to 0 + cold snapshot | 5–10 min | |
| 3 — slow shutdown → 8.4 answering | **49 s** | |
| 4 — verification | ~5 min | |
| 5 — rename | < 1 min | |

## Config, before and after

Paste the Phase 4.1 output verbatim.

```
expected: 0.0.0.0 | utf8mb4_0900_ai_ci | 805306368 | 1000/2000 | redo=268435456
actual:
```

⚠ `innodb_doublewrite_pages` should read **128**, not 4 — deliberate, not pinned.

## The three open questions production answers

The container and scratch-droplet rehearsals both passed these. Production is
the third and only authoritative data point.

| Question | Rehearsal | Production |
|---|---|---|
| Does Oracle's packaging still read `/etc/mysql/mysql.conf.d`? | Yes, all 6 vars held | |
| Does `/etc/mysql/debian.cnf` survive? (logrotate authenticates with it) | Yes | |
| Does the `debian-sys-maint` account survive? | Yes | |

⚠ If `debian.cnf` is gone, the slow query log silently stops rotating and fills
the disk **days later**. Not urgent, not ignorable — file it.

## Deviations from the runbook

Anything you did differently, and why. A deviation that worked is still worth
recording; the next person will hit the same fork.

## Follow-ups raised

| | Ticket |
|---|---|
| Revert `enable_backups` to `false` | TBD-399 |
| Backup hardening (off-host, grants, alerting) | TBD-400 |
| SSH open to `0.0.0.0/0` | TBD-370 |
| Ansible CI + DO dynamic inventory | TBD-206 |
| Remaining `pfv*` names (users, config file, droplet) | TBD-205 / TBD-396 |
