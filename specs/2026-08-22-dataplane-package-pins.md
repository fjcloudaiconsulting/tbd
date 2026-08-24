# TBD-419 — Pin the data-plane packages so the play cannot upgrade MySQL as a side effect

Status: implemented. Date: 2026-08-22.

## Premise, verified

`infra/ansible/roles/common/tasks/main.yml:2-6` ran `ansible.builtin.apt` with
`upgrade: safe` **unconditionally**, as the first task of the first role of
`playbooks/site.yml` (`roles: [common, mysql, redis, backups]`). So running the
play to change a Redis setting also performed an unbounded package upgrade on
`pfv-data-01`, the single node holding all user data and every auth session.

Confirmed at that file and line before any design. The premise is true.

## Measurements this design rests on

All taken in this session, in real `ubuntu:24.04` containers running
`ansible-core`, or against the real repository index. None are assumed.

| # | Measurement | Consequence |
|---|---|---|
| M1 | `ansible.builtin.dpkg_selections` **hard fails** on a package the host does not have: `Failed to find package 'X' to perform selection 'hold'.` (`dpkg --get-selections <absent>` exits 0 but writes "no packages found matching" to stderr, which the module turns into `fail_json`) | The hold list cannot be static |
| M2 | `dpkg_selections` on an installed package is idempotent (`changed` then `ok`), and `apt-mark showhold` then lists it — so `apt-mark hold` and `dpkg --set-selections hold` are the same mechanism | The hand-applied hold converges to `ok`; the operator need do nothing |
| M3 | `dpkg_selections` declares **full check-mode support**: under `--check` it reports `changed` and writes nothing | An ungated read-back is falsely RED on a healthy box |
| M4 | `apt-cache policy <absent-pkg>` exits **0** with **empty stdout** | An empty read must fail closed, never pass |
| M5 | Oracle's noble `mysql-8.4-lts` component publishes `mysql-community-server 8.4.11` **and** a `mysql-server 8.4.11` metapackage with `Depends: mysql-community-server (= 8.4.11)`. `mysql-community-server`'s only `Provides:` is `virtual-mysql-server` — **not** `mysql-server` | The mysql role's `apt: name=mysql-server` is not satisfied by production's installed server |
| M6 | With a dependency held at an older version, `apt-get install <dependent>` prints `E: Unable to correct problems, you have held broken packages` and fails | The hold closes the 9.x door loudly — and also fires on an ordinary patch release, which M5 + this is why the install task had to change |
| M7 | `mysql-innovation` is a live, reachable dist on `repo.mysql.com` | A debconf drift to a 9.x track is reachable, not hypothetical |
| M8 | `roles/backups/tasks/main.yml` uses only `cron`, `file`, `template` — no apt task | `--tags backups` cannot move a package |
| M9 | `ansible.cfg` has no `filter_plugins` key, and `filter_plugins/` auto-loads next to the **playbook** (`playbooks/site.yml`), not next to `ansible.cfg` | The key must be added explicitly or the parser silently never loads |
| M10 | `bin/gen-inventory.py` exits 2 on `--host` without `--private-ip`, and `run-playbook.sh` always passes the flag (possibly empty) | The README's one-flag `--scratch-host` form has never worked |

## The architects' ruling on `upgrade: safe`

Two independent architects, then a concede-or-defend cross. They converged on:

**Retain the task, disarmed with `tags: [patch, never]`.** Not a variable, not a
deletion.

- Not a **variable**: settable from role defaults, `group_vars`, `inventory.yml`
  or the extra-vars temp file `run-playbook.sh` builds — none of which the
  operator is looking at. `never` can only be turned on by typing it, so it
  lands in shell history and in the runbook.
- Not a **deletion**: deleting the capability moves routine patching to a
  hand-run ssh shell block copied out of a closed cutover runbook. That is the
  same undeclared-box-state defect class the ticket exists to kill, at larger
  scale.

**Patch currency lost is small and named.** `unattended-upgrades` is installed
and enabled by this same role and applies `noble-security` daily — unchanged.
The delta is `noble-updates` (non-security SRU bugfixes). Neither path ever
rebooted, so kernel currency is unchanged by any option here (that is a real gap
and a separate ticket). Oracle's `MySQL` origin is not in
unattended-upgrades' allowlist, so nothing automatic could ever have moved
MySQL: the entire major-jump hazard came from `upgrade: safe`.

**`redis-server` is deliberately NOT held.** Architect A proposed it (the
postinst restarts the daemon, and Redis is the auth-session store) and conceded
to B: a hold does not *schedule* a restart, it forbids the move permanently,
with nothing in the repo to notice — permanently blocking security patching on a
VPC-facing service to prevent a major jump that cannot happen (noble ships one
Redis major). The asymmetry is the test: the MySQL holds are free, the Redis
hold is not. Restart policy is filed separately.

## Design

### Where things live

| File | Role |
|---|---|
| `roles/common/defaults/main.yml` | `mysql_hold_candidates` (superset), `mysql_server_detect_order`, `mysql_server_package` (fresh-bootstrap fallback) |
| `roles/common/tasks/holds.yml` | `package_facts` → intersect → `dpkg_selections` → read back → two asserts |
| `roles/common/tasks/mysql_track_fence.yml` | `apt-cache policy` read + track assert |
| `roles/common/tasks/main.yml` | cache refresh; holds; fence; `never`-tagged upgrade; fence re-run |
| `playbooks/site.yml` | `post_tasks` re-applying holds after every role |
| `filter_plugins/apt_policy.py` | the parse, so it can be unit tested |

Holds live in `common` because placement is decided by **tag reachability**, not
topic: the upgrade is now reachable only via `--tags patch`. The holds and the
fence carry `tags: [always]` **and** `apply: {tags: [always]}`, so a tag-limited
run still gets them.

⚠ **Correction (review, 2026-08-23).** This paragraph originally justified the
tags with "`run-playbook.sh`'s own banner documents `-- --tags mysql`" and
claimed the design was "verified by running both invocations". Both were wrong.
Measured with `--list-tasks --tags mysql`: no role in `site.yml` and no task in
`roles/mysql`, `roles/redis` or `roles/backups` carries a topic tag, so that
invocation runs ONLY the `always`-tagged tasks — zero mysql-role tasks. The
banner advertising it has been corrected rather than a tagging scheme invented
in this ticket. The `always` tags remain correct: `--tags patch` is a real
invocation that needs them, and it is the one that can move a package.

⚠ **The cache refresh needed the same treatment and did not have it.** See the
correction under "Subtracted" below.

### The hold set is derived

M1 forbids a static list, and production (Oracle `mysql-community-*`) and a
scratch droplet (Ubuntu `mysql-server`) run different families. The declared
candidate list is intersected with `ansible_facts.packages`.

The intersection is also what creates the vacuous-pin risk: wrong names →
empty set → loop runs zero times → green forever with nothing held. Two
assertions refuse that:

- **read-back** — `dpkg --get-selections` must report every resolved package as
  `hold`. Needed because M2 means the first production converge reports `ok` and
  proves nothing on its own. Gated on `not ansible_check_mode` (M3).
- **anti-vacuity** — the resolved server package must be in the resolved holds.
  Gated on a server actually being installed, so a fresh bootstrap stays green.
  **Not** gated on check mode: a dry run is exactly where wrong names should
  surface.

### The track fence

Invariant: **candidate `major.minor` == installed `major.minor`**, derived from
the host, no version literal anywhere in `that:`/`vars:`. A hardcoded `8.4`
reds every scratch rehearsal against stock Ubuntu, and the reflexive fix is to
widen it to `8.` — one character from permitting 9.0.

Ungated under `--check`, unlike every other fence in this tree, because it
asserts a property of the apt **repository**, not of the converged server.
`--production --check --diff` is the documented pre-flight and is the single run
where finding repo drift matters most.

Re-runs **after** the patch task: the upgrade can itself move
`mysql-apt-config`, whose postinst re-points the repo, so the repo the fence
approved at the top of the play is not necessarily the one in force at the
bottom.

**Accepted limit, stated rather than hidden:** if installed and candidate both
move to the same new track, this passes. That is a deliberate major upgrade — a
windowed operation with a snapshot — not the accident the fence is for.

### The install task had to change

M5 + M6: with `mysql-community-server` held at 8.4.11, a hardcoded
`apt: name=mysql-server state=present` resolves `mysql-server 8.4.12` on an
**ordinary patch release**, whose `Depends: mysql-community-server (= 8.4.12)`
cannot be met, and the play dies with a message naming the hold rather than the
cause. Targeting `mysql_resolved_server_package` makes `state: present` the
no-op it was always meant to be, while leaving the hold to fire on a drifted
repo.

### Corrections from review (2026-08-23)

Three defects in the play itself, all found by review before the branch merged.

**`intersect` discards order, so the resolved server package was
nondeterministic.** `mysql_resolved_server_package` read a POSITIONAL element
(`| first`) off `mysql_server_detect_order | intersect(...)`, and ansible-core
implements `intersect` as `list(set(a) & set(b))`; set iteration order over
strings depends on `PYTHONHASHSEED`, which is randomised per process. Measured
six times against ansible-core 2.21.3 with identical inputs: `mysql-server`
three times, `mysql-server-8.0` three times. Every stock-Ubuntu host has more
than one detect-order name installed (`apt install mysql-server` on noble brings
in both), so the package the mysql role installed and the package the track
fence probed flipped between converges of an unchanged host. Replaced with
`select('in', ...) | list | first`, which filters the detect order in place;
verified deterministic over four runs and across all three host shapes.
`mysql_resolved_holds` and `mysql_installed_server_present` keep `intersect`
deliberately — neither reads a positional element.

⚠ This also made `test_the_hold_candidate_list_covers_both_mysql_package_families`
vacuous: it asserted `detect[0] == "mysql-community-server"` on the rationale
that detection "must prefer the Oracle package", certifying an ordering the
runtime threw away. That assertion now means something, and a companion fence
rejects a bare `intersect(...) | first` for this key.

**The cache refresh was untagged.** See the correction under "Subtracted".

**`post_tasks` do not run after a role failure.** The stated guarantee — "a host
that had MySQL installed BY THIS RUN does not finish the run unheld" — failed on
the play's own documented failure mode, the 2026-08-18 scratch run where the
redis role failed after the mysql role had installed MySQL (`site.yml:5-10`).
That host finished unheld. Fixed by re-applying the holds **at the install
site**, immediately after `Install MySQL server` in the mysql role, rather than
at the end of the run: that task is the only one in the play that can put MySQL
on a box, so the window between "MySQL exists" and "MySQL is held" now contains
nothing that can fail. Wrapping `site.yml`'s roles in a `block:`/`always:` was
the alternative and was rejected — it requires converting `roles:` into
`include_role`/`import_role` tasks on the play that provisions the production
data plane, and it still only re-holds at the end of the run, a strictly weaker
guarantee. The `post_tasks` pass stays as the backstop for a future role.

**Latent, documented, not fixed: `Multi-Arch: same`.** `dpkg --get-selections`
prints the arch qualifier for such packages (measured: `libext2fs2t64:arm64`)
while `package_facts` keys them plain, so the read-back's `difference()` would
report one as unheld and abort every converge. None of the 13 current candidates
is `Multi-Arch: same`; `libmysqlclient24` (which is) is exactly what someone
would add next, so the hazard is written next to the list rather than
restructured around.

## Fences

`backend/tests/` so they ride the existing CI shards — no new CI job, no
`needs:` wiring. `docker-compose.yml` gains `./infra:/app/infra:ro`, keeping the
`GITHUB_ACTIONS` RuntimeError, so the fences can be inject-and-confirm-red
validated locally instead of being CI-only.

`test_apt_policy_filter.py` — the one with teeth. A YAML fence cannot tell a
correct parse from `'8.4' in stdout`, and the substring version **passes on a
host whose Candidate is 9.0.1**, because `apt-cache policy` prints the whole
version table and 8.4.11 is still in it. `POLICY_DRIFTED_TO_9` is that fixture,
plus a meta-test asserting the fixture still traps the bug.

`test_dataplane_apt_pins.py` — the shape fences, all `yaml.safe_load`, never
grep: the role now carries a comment containing the literal string
`upgrade: safe`, so a whole-file grep is satisfied by the comment documenting
its own absence.

⚠ **Review found ELEVEN of these green under the wrong implementation** (see
the corrections section above for the three play defects; these are the fence
defects). The pattern in almost every case was a fence asserting on a
CONTAINER rather than on the thing inside it: `"never" in tags` rather than the
tag SET (`always` beats `never` in ansible's own evaluation, so
`[patch, never, always]` re-armed the defect and stayed green); `set(filters)`
rather than the mapping's VALUES (a swapped mapping makes the whole repo-drift
fence a tautology); a string search over `that:` + `vars:` rather than the
`that:` CLAUSES (the entire assertion could be deleted with the filters left
wired); `str(<the whole set_fact args dict>)` rather than the one key under
test (two sibling keys satisfied it for free); `_flatten` stripping ancestors'
`ignore_errors`/`rescue` off the children it pulls up; a glob that never read
role HANDLER files; a sweep of module arguments that could not see
`module_defaults`, which is a keyword, not a module. Every fence changed here
was validated by injecting the implementation it names, confirming RED, and
restoring — 27 mutations, plus 3 CORRECT edits confirmed still GREEN, because
three of these assertions were also over-specified and would have reddened a
legitimate change (an added fourth filter, a `that:` clause naming
`mysql-server-8.0`, deduping `python3-pymysql` out of the mysql install task).

## Subtracted

- **`redis-server` from the hold set** — architect ruling above.
- **A `mysql_allowed_tracks` version-literal floor** — catches only "already on
  9.x", a post-hoc complaint; candidate-vs-installed prevents the arming, and a
  literal goes stale mid-window.
- **The `debconf-show mysql-apt-config` selection assert and the
  `debconf-utils` package it needs.** Both architects wanted it. It is
  redundant with detection already in place: a drifted selection reaches the
  sources list, the cache refresh runs before the fence, and the fence then sees
  the changed candidate. The only window it uniquely covered was the deliberate
  unhold-and-upgrade moment, which the post-patch fence re-run now covers for
  free. Not worth adding a package to production's baseline.

  ⚠ **Correction (review, 2026-08-23).** "The play's own `update_cache` runs
  before the fence" was only true of an untagged, non-check run. As first
  written the cache-refresh task carried no tag, so `--list-tasks --tags patch`
  listed the holds, both fence passes and the upgrade but NOT the refresh: on
  the one invocation that upgrades, the fence read whatever was already on disk
  and the cache was refreshed afterwards by the upgrade task itself. The
  documented `--production --check --diff` pre-flight had the same gap, because
  `ansible.builtin.apt` guards its refresh with `if not module.check_mode`. The
  refresh now carries `tags: [always]`, which makes the sentence above true on
  every invocation. The subtraction stands on its own merits — do **not** re-add
  the debconf assert.

## What the operator must do on the droplet

**Nothing, for the change to converge.** M2: the hand-applied
`apt-mark hold mysql-apt-config` is the same dpkg mechanism `dpkg_selections`
writes, so the first converge adopts it and reports `ok`.

One **read-only** check is worth running, to settle whether the install-task
defect (M5/M6) was live or latent on this box:

```bash
dpkg -l mysql-server mysql-community-server
apt-mark showhold
```

If Oracle's `mysql-server` metapackage is absent, the defect was live and the
fix landed just in time; if present, it was latent and a rebuild would have
reproduced it. Either way the fix is correct.
