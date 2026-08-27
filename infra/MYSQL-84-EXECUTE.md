# TBD-360 — execution sheet (copy/paste)

**This is the do-it-now sheet.** For *why* any step exists, read
[`MYSQL-84-CUTOVER.md`](MYSQL-84-CUTOVER.md); for the analysis, the spec beside
it. This file assumes you have already decided to go and are sitting at your
laptop with `doctl`, `terraform`, `ansible` and your SSH key.

**Measured 2026-08-19: ~24 min total, of which ~8 min database-down.** The
window is dominated by the credential rotation and its deploy, the cold
snapshot, and the package swap. ⚠ There is **no App Platform scaling** — see
2.1, it is not available on this plan — and the database is down for far longer
than the "~1 minute" this file used to claim, because the snapshot is now taken
with MySQL already stopped.

> **No production identifiers appear in this file.** Every IP, droplet id and
> app id is derived at run time. That keeps it safe in a public repo *and*
> keeps it correct if anything is ever rebuilt.

---

## Conventions

- Every block is copy/paste as-is into **one** terminal, in order.
- `✅ EXPECT:` lines tell you what a good result looks like. **If you do not see
  it, stop.** Do not proceed on "probably fine".
- 🌐 marks a **web-console fallback** for steps that can be done without the CLI.
- 🔙 marks a rollback point.

---

## Phase 0 — Session setup (no outage, do this first)

```bash
cd ~/src/tbd
git checkout main && git pull --ff-only

export TFDIR=infra/terraform
export DROPLET_ID=$(terraform -chdir=$TFDIR output -raw droplet_id)
export DROPLET_IP=$(terraform -chdir=$TFDIR output -raw droplet_public_ipv4)
export APP_ID=$(doctl apps list --format ID,Spec.Name --no-header | awk '$2=="pfv"{print $1}')
export SSH_KEY=~/.ssh/id_rsa.home
export SSHQ="ssh -o BatchMode=yes -o ConnectTimeout=10 -i $SSH_KEY root@$DROPLET_IP"

echo "droplet=$DROPLET_ID ip=$DROPLET_IP app=$APP_ID"
```

✅ EXPECT: three non-empty values. An empty `APP_ID` means `doctl` is
unauthenticated — fix that before going further.

### 0.1 Prove every access path works *now*, not mid-window

```bash
$SSHQ 'echo SSH_OK; mysql --no-defaults -N -B -e "SELECT VERSION()"'
$SSHQ 'mysql -N -B -e "SELECT CURRENT_USER()"'   # NO --no-defaults, on purpose
terraform -chdir=$TFDIR output -json | python3 -c 'import json,sys; d=json.load(sys.stdin); print("TF secrets present:", all((d.get(k) or {}).get("value") for k in ("mysql_app_password","mysql_backup_password","redis_password")))'
doctl compute droplet backups $DROPLET_ID --format Name,Created --no-header | head -3
```

✅ EXPECT: `SSH_OK`, `8.0.46-...`, `TF secrets present: True`, and **at least one
backup row**.

The second line is the one that is easy to skim past. It is the *only* step in
this sheet that runs a bare `mysql` before the one-way door — everything else
uses `--no-defaults`, which by design cannot see the problem.

✅ EXPECT **before** Phase 1: `pfv_backup@localhost`. That is the known live
footgun, and Phase 1 fixes it. ✅ EXPECT **after** Phase 1: `root@localhost`.

⚠ Anything else — a third user entirely — means a `[client]` section in a file
the play does not manage (`/etc/mysql/my.cnf`, `/root/.mylogin.cnf`). Phase 1's
own fence would catch it, but it would catch it *mid-play*, after the
credentials have rotated and before the redis role has run. Find it now.

⚠ If there is no backup row, **stop**. Enabling backups creates nothing; the
policy is `daily, hour 0`, so you may be waiting until the next 00:00 UTC.

### 0.2 Record your rollback coordinates

```bash
doctl compute droplet backups $DROPLET_ID --format ID,Name,Created --no-header | head -1
```

📋 **Write the backup ID down.** A *backup* restores with
`doctl compute droplet-action restore --image <id>`; a *snapshot* (Phase 2) uses
`rebuild`. Both preserve the droplet id and both IPv4s. Reaching for the wrong
verb mid-outage is a hard stop.

### 0.3 Dry run — changes nothing

```bash
rm -f /tmp/tbd360-dryrun.txt                  # see the umask note below
( umask 077; : > /tmp/tbd360-dryrun.txt )
./infra/ansible/bin/run-playbook.sh --production --check --diff 2>&1 | tee -a /tmp/tbd360-dryrun.txt
grep -E "PLAY RECAP|^pfv-data-01" /tmp/tbd360-dryrun.txt
less /tmp/tbd360-dryrun.txt      # read the diff, then:  rm -f /tmp/tbd360-dryrun.txt
```

⚠⚠ **That file contains two production secrets in cleartext.** `--check --diff`
rendered the template diffs. Since TBD-414 both secret-bearing tasks carry
`no_log: true`, so this no longer applies to them. Historically: the
rotated `mysql_backup_password` (`roles/mysql/templates/root.my.cnf.j2`) and the
rotated `redis_password` (`roles/redis/templates/00-static.conf.j2`) are both in
the payload you are being asked to read. Hence the subshell `umask 077`, and
hence `rm -f` when you are done. `run-playbook.sh` goes to some trouble to keep
these off disk (mode-0600 mktemp, trapped on every exit path); do not undo that
with a redirect.

⚠ `rm -f` **first**, and create the file under the umask before writing to it.
`umask` governs `open(O_CREAT)` only: redirecting onto a path that already
exists is `O_TRUNC` and leaves the inode's mode alone, so a file left behind by
an earlier run — this sheet's previous revision said to `tee` here — stays
world-readable no matter what umask you wrap the command in.

⚠ `tee`, not a bare redirect. This run includes the `common` role's apt task,
the slowest thing in the sheet, and under a redirect you watch a blank terminal
for minutes. Worse, anything the wrapper prompts for — an encrypted SSH key
passphrase, a TFC re-auth — goes to the file instead of the screen, which reads
as a hang with no explanation.

✅ EXPECT: `failed=0`. Some `changed` is normal — the three user tasks rewrite
every run by design (`plugin_auth_string` is cleartext and cannot be compared).

⚠ The play's verification fences are **skipped under `--check`**, deliberately.
They assert properties of the *converged* server, and `--check` converges
nothing: under `--check` the `command` module self-reports `skipped` with
`stdout` left at `''` rather than running the query, so the reads come back
empty and the asserts fail on a healthy box. Measured against production
2026-08-18 — the dry run reported `failed=1` on
`Assert a bare mysql run as root IS root`, whose fail message named the
authenticated user as `"nothing"`: an empty read, not a real identity. They are
gated on `not ansible_check_mode`; a real run still asserts.

**Read the diff, do not just count the failures.** Measured against production
2026-08-18 **on the gated play** — the `failed=1` observation above predates the
gating and came from a run that never reached the redis and backups roles.
`--check --diff` reported `changed=10`, and every one was expected. ⚠ That count
predates TBD-419: the first row below no longer runs on a default converge, so
expect one fewer.

| Changed | Meaning |
|---|---|
| ~~`Update apt cache and upgrade packages`~~ | **Gone since TBD-419.** That task was split: the cache refresh still runs (and reports no change), and the upgrade half is now `Upgrade OS packages`, tagged `[patch, never]`, so it does not appear in a default run at all. What was measured here — 27 upgrades + 8 new, kernel included, no mysql/redis packages on that day — is what a `--tags patch` run would now show. See 0.4 |
| `Set system timezone` | `Etc/UTC` → `UTC`; cosmetic, rewrites every run |
| `Create backup user` + 2 × `Create application MySQL user` | the password rotation + `caching_sha2_password` conversion |
| `Drop /root/.my.cnf` | removes the `[client]` section — the footgun is **still live on production** |
| `Drop pfv MySQL config override` | drops `default-authentication-plugin`, and restates `innodb_log_file_size 128M` as `innodb_redo_log_capacity 256M` (**same 268435456 bytes** — 128M × the default `innodb_log_files_in_group=2`; a restatement, not a resize). **Notifies a MySQL restart** |
| `Drop pfv Redis static config override` | `requirepass` rotation. **Notifies a Redis restart.** ⚠⚠ **This task is `no_log: true` since TBD-414, so its diff is CENSORED and the check below can no longer be done from the dry run.** The same file carries `bind 127.0.0.1 <private_ipv4>`, and a change there means Redis stops listening on the address App Platform uses. Verify `bind` by reading `roles/redis/templates/00-static.conf.j2` against the inventory *before* the run, and rely on the role's live `bind` fence, which runs after apply |
| 2 handlers | the two restarts above |

⚠⚠ **Phase 1 therefore restarts BOTH MySQL and Redis while the app is still
serving.** The sheet quiesces in Phase 2, not Phase 1. Sessions survive the
Redis restart (AOF is on, deliberately — Redis is the auth-session store), but
every client connection is dropped and the admin dashboard reports
`Redis: DOWN` until the pool reconnects — *and* `requirepass` has rotated, so it
cannot reconnect at all until `REDIS_URL` is updated in 1.2.

⚠ You cannot avoid this by scaling the backend to zero first — that is not
available on this plan (2.1). Accept the blip, and keep 1.2 short.

### 0.4 Take the OS package upgrade OUTSIDE the window

⚠ **Superseded by TBD-419 (2026-08-22): the premise of this step is no longer
true.** The `common` role used to run `apt upgrade: safe` **unconditionally**,
so a stale box dragged an unbounded apt run — kernel included — into the middle
of Phase 1. That task now carries `tags: [patch, never]` and runs only when
`--tags patch` is typed on the command line, and the MySQL packages are held in
the dpkg database, so a routine converge moves no packages at all. Phase 1 is
already purely a credentials-and-config step; nothing here is required to make
it one.

Keep the step anyway, as **elective OS currency**: `unattended-upgrades` applies
`noble-security` daily but not `noble-updates`, and neither path reboots. If you
run it, run it in advance for the same reason as before — it is a real apt run
with a reboot at the end. If you skip it, Phase 1 is unaffected. The MySQL and
Redis packages are held either way, so the "stop if anything matches" gate below
is now a second layer rather than the only one.

⚠ **The hold makes this step's output different.** Held packages are reported as
`kept back`. That is correct and expected here — it is not the `kept back` that
`--with-new-pkgs` fixes (see the note below), and it must not be cleared by
unholding. To move MySQL deliberately, use the procedure in `infra/MIGRATION.md`,
"Data-plane package pins".

⚠ **This step is itself a short blip, not a free one.** Budget a few minutes at
a quiet moment; it ends in a deliberate reboot (see below).

**First, prove it is safe to do outside the window.** This only simulates:

```bash
$SSHQ 'set -e
apt-get update -qq
apt-get -s upgrade --with-new-pkgs > /tmp/tbd360-sim.txt
grep -Ei "^(Inst|Conf) .*(mysql|redis|libmysql)" /tmp/tbd360-sim.txt || echo "  no mysql/redis packages in the upgrade (good)"
rm -f /tmp/tbd360-sim.txt'
```

⚠ `set -e`, and the simulation goes to a file rather than into a pipe. The
obvious one-liner — `apt-get update && apt-get -s upgrade | grep ... || echo
good` — **fails open**: a pipeline binds tighter than `&&`, so a failed
`apt-get update` (expired key, mirror 5xx, the dpkg lock held by the
unattended-upgrades timer this role enables) short-circuits to the `||` and
prints the reassuring line having proved nothing. Discarding the simulation's
stderr does the same. This gate's only acceptable failure mode is a false stop.

✅ EXPECT the `good` line. ⚠ **If anything matches, stop.** Upgrading MySQL or
Redis here restarts production's database with the backend still serving, before
the Phase 2 snapshot exists and without Phase 3.1's slow-shutdown discipline.
That work belongs inside the window, after the snapshot.

```bash
$SSHQ 'set -e
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
apt-get update -qq
set +e
apt-get -y --with-new-pkgs -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold upgrade
echo "apt-exit=$?"'
```

✅ EXPECT `apt-exit=0`, and no `kept back` line **other than the held MySQL
packages** (TBD-419 — see the hold note in 0.4; those are supposed to be kept
back and unholding them here is exactly what `infra/MIGRATION.md` forbids).

⚠ `--with-new-pkgs` is what makes this equivalent to the role's `--tags patch`
path (`ansible.builtin.apt` maps `upgrade: safe` to
`apt-get upgrade --with-new-pkgs`, and since TBD-419 that task runs only under
that tag). Without it, apt holds back every package that needs a *new* one
installed — which is exactly the 8 new kernel packages. ⚠ The second half of
this note is now stale: since TBD-419 the play's apt task is `never`-tagged, so
0.3's default dry run no longer reports it at all, `changed` or otherwise.

⚠ `export`, not a `VAR=x cmd` prefix: an assignment prefix binds to the single
command it precedes, so the old form set the frontend on `apt-get update` (which
never prompts) and not on the upgrade (which can). `$SSHQ` carries
`-o BatchMode=yes` and no tty, so a debconf prompt there is a hang holding the
dpkg lock, not a question.

⚠ Do not pipe this to `tail`. The `kept back` list, the package names, and any
`dpkg: error processing` all scroll past in the middle, and a pipeline's exit
status is the *last* command's, so `$?` would be `tail`'s and always 0.

**Then reboot, deliberately, here rather than inside the window:**

```bash
BOOT_BEFORE=$($SSHQ 'cat /proc/sys/kernel/random/boot_id')
$SSHQ 'if [ -e /var/run/reboot-required ]; then echo REBOOTING; systemctl --no-block reboot; else echo "NO REBOOT NEEDED"; fi'

until $SSHQ "test \"\$(cat /proc/sys/kernel/random/boot_id)\" != '$BOOT_BEFORE'" 2>/dev/null; do
  echo "waiting for reboot..."; sleep 10
done
$SSHQ 'uptime; uname -r; systemctl is-active mysql redis-server
ls /var/run/reboot-required 2>/dev/null || echo "reboot-required cleared (good)"'
```

✅ EXPECT `REBOOTING`, then the loop clearing, then both services `active` and
`reboot-required cleared (good)`.

⚠ The loop compares **boot ids**, because that is the only positive proof the
box actually rebooted. `uname -r` cannot tell you — nobody recorded the old
value — and an unconditional "requested" message cannot either: if
`/var/run/reboot-required` is absent, nothing reboots and every downstream
check still passes. Expect `waiting for reboot...` a few times; a 1-vCPU
droplet stopping MySQL and Redis and booting a fresh kernel to sshd lands
around 35-75s, so connection refusals during that stretch are the loop working,
not a dead droplet.

⚠ Rebooting now is the point, not a side effect. `NEEDRESTART_MODE=l` above
tells needrestart to *list* services rather than restart them — a library
upgrade (libssl, libc, libkrb5) can otherwise restart mysqld and redis-server
even though no mysql or redis *package* was upgraded, which is a surprise
restart of production's database in a step labelled "no outage". The reboot
replaces that with one you chose. It also means Phase 2 snapshots a system whose
new kernel has already been proven to boot: deferring the first boot to the
power-cycle in 2.2 puts it inside the outage, on the machine that then has to
survive an irreversible package swap, and makes your primary undo a snapshot of
an unproven kernel.

**Now re-run 0.3.** The sheet is copy/paste in order, and this is the step that
makes 0.4 verifiable:

```bash
rm -f /tmp/tbd360-dryrun.txt; ( umask 077; : > /tmp/tbd360-dryrun.txt )
./infra/ansible/bin/run-playbook.sh --production --check --diff 2>&1 | tee -a /tmp/tbd360-dryrun.txt
grep -E "PLAY RECAP|^pfv-data-01" /tmp/tbd360-dryrun.txt
# once you have read the number below:  rm -f /tmp/tbd360-dryrun.txt
```

✅ EXPECT `failed=0` and **`changed=9`**, not the `changed=10` in the table
above: the apt row is gone and the other nine are unchanged. That drop from 10
to 9 *is* the evidence that 0.4 worked.

---

## Phase 1 — Credentials + config  ⚠ SHORT OUTAGE

This converts the three accounts to `caching_sha2_password`, removes the
`default-authentication-plugin` line 8.4 refuses to boot with, and **rotates the
passwords to the Terraform-generated values**. The app keeps using the old
password until 1.2 — expect a gap.

⚠⚠ **RUN THE PLAY FIRST, THEN 1.2. The order is load-bearing, not stylistic.**
Doing 1.2 first fires a deploy whose `migrate` PRE_DEPLOY job authenticates with
the *new* password against a server that still has the *old* one. It fails at
6/12, App Platform keeps the previous deployment alive (so the app stays up on
the old container, with the old credentials, which then break the moment the
play lands), and a second deploy is needed. Measured 2026-08-19: about seven
minutes of avoidable downtime.

```bash
./infra/ansible/bin/run-playbook.sh --production
```

✅ EXPECT: `failed=0`, and the play's own final task
`Assert the RUNNING server has the intended configuration` passing. That task
reads the live server back — if it passes, the config is genuinely in effect,
not merely on disk.

### 1.1 Verify by failure mode, not by allowlist

```bash
$SSHQ 'mysql -N -B -e "SELECT CURRENT_USER()"'   # NO --no-defaults; EXPECT root@localhost now
$SSHQ 'mysql --no-defaults -t -e "SELECT user, host, plugin, LENGTH(authentication_string) hash_len FROM mysql.user WHERE plugin <> \"caching_sha2_password\""'
$SSHQ 'mysql --no-defaults -t -e "SELECT user, host, LENGTH(authentication_string) hash_len FROM mysql.user WHERE user LIKE \"pfv%\""'
$SSHQ 'grep -rn "default.authentication.plugin" /etc/mysql/ || echo "  ABSENT (good)"'
```

✅ EXPECT: `root@localhost` from the first line — this is the other half of
0.1's identity probe, and the only place the sheet proves the footgun is gone
before Phase 3.1. Then the first query returns **only
`root@localhost / auth_socket`**. Second
shows `hash_len = 70` for all three `pfv_*` rows — a `0` means the passwordless
form got in. Third prints `ABSENT` (comments do not count; look for a real
setting line).

⚠ Anything else in the first query — a monitoring user, a hand-made login — is
an account that **stops authenticating the moment 8.4 starts**, mid-window,
after the point of no return. Convert it now or delete it.

### 1.2 Re-encrypt BOTH `DATABASE_URL` bindings

⚠⚠ **There are two, and they are separate encrypted values**: the `backend`
service and the `migrate` PRE_DEPLOY job. Missing the job's copy does not fail
now — it fails at the **next deploy**. Same for `REDIS_URL`.

```bash
terraform -chdir=$TFDIR output -raw mysql_app_password   # copy, do not echo into notes
terraform -chdir=$TFDIR output -raw redis_password
terraform -chdir=$TFDIR output -raw droplet_private_ipv4
```

🌐 **Console (recommended here — fewer moving parts):**
Apps → `pfv` → Settings → `backend` → Environment Variables → edit
`DATABASE_URL` and `REDIS_URL` → **repeat for the `migrate` job** → Save. Saving
triggers a deploy.

**CLI alternative:** put the plaintext values into `.do/app.yaml`, then
`doctl apps update $APP_ID --spec .do/app.yaml`. DigitalOcean re-encrypts them
and returns `EV[...]`; fetch the spec back and commit those blobs
(`doctl apps spec get $APP_ID > .do/app.yaml`). **Never commit plaintext.**

```bash
curl -s https://$(doctl apps get $APP_ID --format DefaultIngress --no-header | sed 's|https://||')/ready
curl -s https://$(doctl apps get $APP_ID --format DefaultIngress --no-header | sed 's|https://||')/health/dependencies
```

✅ EXPECT: `/ready` → `database: connected`, and `/health/dependencies` →
`{"status":"ok","checks":{"database":"ok","redis":"ok"}}`.

⚠ Check **both**. `/ready` is the database-only rotation gate: it was green
throughout the 2026-08-19 window on an app where no user could log in, because
Redis was enforcing a stale password. `/health/dependencies` (TBD-413) is the
one that covers Redis, and a 503 from it with `"redis":"auth_failed"` is
exactly the credential shape this phase can produce.

🔙 **ROLLBACK (still cheap here):** on 8.0 you can put the accounts back with
`ALTER USER 'pfv_app'@'%' IDENTIFIED WITH mysql_native_password BY '<old>';` and
restore the previous secrets. Nothing irreversible has happened yet.

---

## Phase 2 — Quiesce and snapshot  ⚠ OUTAGE STARTS

### 2.1 Quiesce the app

⚠⚠ **SCALING TO ZERO IS NOT AVAILABLE ON THIS APP.** The instruction that used
to be here could not be carried out, and nobody discovered that until the middle
of the 2026-08-19 window. Two separate failures:

* **Console:** the `backend` component runs on the legacy `basic-xxs` plan,
  which pins it to exactly one container ("This plan is limited to 1 container.
  Plans starting at $12.00/mo can manually scale or autoscale"). A visible
  refusal.
* **CLI:** `doctl apps update` with `instance_count: 0` was **not "not
  accepted" — it was silently ignored.** `0` is Go's zero value and is dropped
  by `omitempty` before the request leaves the client, so the command exits 0
  and prints a plausible spec while nothing changes. Plan-independent; no tier
  fixes it. This is why it read as tested.

⚠⚠ **SO NOTHING QUIESCES THE APP. Be honest with yourself about this rather
than assuming something upstream handled it.** By the end of 1.2 the backend
has working credentials again — that is the whole point of 1.2's `/ready`
check — so it serves writes normally right up until MySQL stops in 2.2.

**The exposure is the interval from 1.2 to the shutdown in 2.2.** Any write
taken in it is lost **if you later roll back to the snapshot**, because the
snapshot is taken after it. Nothing else is at risk: the write itself succeeds,
and if you never roll back it is simply a normal write.

**Therefore: go from 1.2 straight into 2.2.** Do not break for coffee here.
Measured 2026-08-19, the interval was about two minutes and the accepted risk
was judged negligible for this app's traffic.

⚠⚠ **This paragraph used to name two "real levers" — a plan bump and dropping
the cloud-firewall rules. Both were ruled out on evidence in TBD-416; do not
reach for either.**

* **Plan bump** buys nothing for the CLI route: `doctl` discards
  `instance_count: 0` client-side under `omitempty`, before any pricing rule is
  consulted.
* **Firewall rules are the WORST option, not the middle one.**
  `backend/app/database.py:25-27` records that aiomysql 0.2.0 accepts no
  read/write timeouts, so dropping the 3306 rule either leaves established
  flows serving writes (stateful conntrack, quiescing nothing) or orphans a
  mid-transaction connection with no RST that then holds its metadata locks
  until the stock 8h `wait_timeout` — manufacturing the exact blocker a quiesce
  exists to remove. It also mutates a Terraform-owned resource against the
  VCS-only rule.

What replaced them is to bound the waiting rather than stop the writers: see
**"Quiescing without scaling to zero"** in `infra/MIGRATION.md`.

```bash
curl -s https://$(doctl apps get $APP_ID --format DefaultIngress --no-header | sed 's|https://||')/ready
curl -s https://$(doctl apps get $APP_ID --format DefaultIngress --no-header | sed 's|https://||')/health/dependencies
```

✅ EXPECT `database: connected` **if** you completed 1.2 — that is the state you
want going in, because it means both bindings are proven. The database goes down
in 2.2, not here.

⚠ `/health` stays `200` throughout: it is a liveness probe and does not touch
the database. Do not read it as "the app is fine". `/ready` does check the
database — but **not Redis**, deliberately, because it is the rotation gate.
Read `/health/dependencies` (TBD-413) for Redis: it reports both dependencies
and returns 503 when either is unusable. Before that endpoint existed, every
external signal said healthy on an app where nobody could log in.

### 2.2 Cold snapshot, powered off

⚠ **Shut MySQL down cleanly FIRST — run 3.1 before the power-off, not after.**
This sheet used to power off a running server, which makes the snapshot
crash-consistent. Running the slow shutdown first makes it an image of a
cleanly-closed InnoDB, which is a strictly better thing to roll back to, and it
costs one command.

⚠ Then run **3.1 again** after the power-on. MySQL auto-starts on boot, so
between power-on and the package swap there is a window in which the app can
take writes that a rollback would discard. Keep it short.

```bash
doctl compute droplet-action power-off $DROPLET_ID --wait
doctl compute droplet-action snapshot $DROPLET_ID --snapshot-name "tbd360-pre-84-$(date +%Y%m%d-%H%M)" --wait
doctl compute droplet-action power-on $DROPLET_ID --wait
doctl compute snapshot list --resource droplet --format ID,Name,Created --no-header | head -2
```

📋 **Write the snapshot ID down.** This is your primary undo from here on.
Measured 2026-08-19: **58 s** for the snapshot and 2 min 27 s for the whole
power-off → snapshot → power-on cycle, on this 25 GB disk. The old "5–10
minutes" estimate was never measured.

🔙 **ROLLBACK from here on:**
`doctl compute droplet-action rebuild $DROPLET_ID --image <snapshot-id> --wait`
(**`rebuild`** for a snapshot; `restore` is for a backup). Preserves the droplet
id and both IPv4s, so the private address App Platform is pinned to survives.

---

## Phase 3 — The cutover  ⚠⚠ ONE-WAY DOOR

**In-place downgrade from 8.4 to 8.0 is not supported.** Past this point the
only way back is the snapshot.

### 3.1 Slow shutdown

⚠⚠ **YOU ARRIVE HERE TWICE, BOTH TIMES FROM 2.2 — and the first time is BEFORE
the snapshot exists.** Once to shut MySQL down cleanly so the snapshot images a
closed InnoDB, and again after the power-on, because MySQL auto-starts on boot.
**Do not run straight on into 3.2 the first time.** ↩ Return to 2.2 after the
`STOPPED (good)` line; 3.2 is the one-way door and the snapshot is your only
way back through it.

⚠⚠ **Keep `--no-defaults`, even though the root cause is now fixed.**

The `[client]` section was removed from `/root/.my.cnf` (see
`roles/mysql/templates/root.my.cnf.j2`), and Phase 1's play asserts that a bare
`mysql` run as root really is `root@localhost`. So on a freshly-played box this
flag is belt-and-braces.

It stays in this sheet for three reasons, each of which is live during a window:

1. **Production has not been played yet** when you start. The old
   `/root/.my.cnf` with its `[client]` section is still there until Phase 1
   completes.
2. **A rollback to the Phase 0.2 BACKUP restores the old file**, and with it
   the trap. ⚠ Not the Phase 2 snapshot — that is taken *after* Phase 1 played
   the box, so it carries the fixed `/root/.my.cnf`. Getting these two the
   wrong way round mid-window is easy and expensive.
3. The template only governs `/root/.my.cnf`. A `[client]` section in
   `/etc/mysql/my.cnf` or `~/.mylogin.cnf` would do the same thing, and nothing
   rewrites those.

Without it, a bare `mysql` as root authenticates as the low-privilege backup
user and this step fails with `ERROR 1227` — then dpkg stops mysqld with the
*default fast* shutdown and the DD upgrade starts from a non-clean state. It
looks survivable, and it is exactly the failure this step exists to prevent.

```bash
$SSHQ 'mysql --no-defaults -N -B -e "SELECT CURRENT_USER()"'
$SSHQ 'mysql --no-defaults -e "SET GLOBAL innodb_fast_shutdown = 0;" && echo SET_OK'
$SSHQ 'mysqladmin --no-defaults shutdown; sleep 5; systemctl is-active mysql || echo "STOPPED (good)"'
```

✅ EXPECT: `root@localhost`, then `SET_OK`, then `STOPPED (good)`.
⚠ If the first line prints `pfv_backup@localhost`, your `--no-defaults` did not
take — stop and fix it before shutting anything down.

### 3.2 Package swap to Oracle 8.4

⚠ Use `mysql-apt-config`. Do **not** hand-add the repo key: the standalone
`RPM-GPG-KEY-mysql-2023` expired 2025-10-22 and apt then rejects the repo with
`EXPKEYSIG`, which surfaces as the very misleading
`E: Unable to locate package mysql-community-server`.

```bash
$SSHQ 'export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq lsb-release wget gnupg debconf debconf-utils >/dev/null
curl -fsSL -o /tmp/mac.deb http://repo.mysql.com/apt/ubuntu/pool/mysql-apt-config/m/mysql-apt-config/mysql-apt-config_0.8.39-1_all.deb
cat <<SEL | debconf-set-selections
mysql-apt-config mysql-apt-config/repo-distro select ubuntu
mysql-apt-config mysql-apt-config/repo-codename select noble
mysql-apt-config mysql-apt-config/repo-url string http://repo.mysql.com/apt
mysql-apt-config mysql-apt-config/select-server select mysql-8.4-lts
mysql-apt-config mysql-apt-config/select-connectors select Disabled
mysql-apt-config mysql-apt-config/select-product select Ok
SEL
dpkg -i /tmp/mac.deb && apt-get update -qq
apt-cache policy mysql-community-server | head -3'
```

✅ EXPECT: a real `Candidate:` such as `8.4.11-1ubuntu24.04`, **not `(none)`**.
⚠ `apt-get update` exits **0** against a repo whose signature failed, so its
exit status proves nothing. Gate on the candidate.

```bash
$SSHQ 'export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
apt-get install -y -o Dpkg::Options::=--force-confold mysql-community-server > /tmp/tbd360-install.log 2>&1
echo "install-exit=$?"
tail -15 /tmp/tbd360-install.log'
```

✅ EXPECT `install-exit=0`. Piping the install straight to `tail` reports the
pipeline's status, which is `tail`'s and always 0 — the full log stays on the
box instead.

⚠ Expect to see `update-alternatives: using /etc/mysql/mysql.cnf to provide
/etc/mysql/my.cnf` in that tail. That is Oracle's packaging repointing the file
that carries the `!includedir`, and it is exactly the event 4.1 exists to catch.
Measured 2026-08-19: the include survived it and all six pinned values held —
but that is a measurement, not a guarantee, so still run 4.1.

⚠ **Do not run `mysql_upgrade`** — removed in 8.4. The server performs the
data-dictionary upgrade itself at startup.

```bash
$SSHQ 'systemctl start mysql 2>/dev/null || systemctl start mysqld; sleep 5; mysql --no-defaults -N -B -e "SELECT VERSION()"'
```

✅ EXPECT: `8.4.x`. Measured elapsed for 3.1→here on a rehearsal droplet: **49
seconds**.

---

## Phase 4 — Verify before letting traffic back

### 4.1 Config include path — assert BY NAME

⚠ `mysqld --validate-config` **cannot** detect a dropped include: a file that is
never read still validates. Only the live values prove it.

```bash
$SSHQ 'mysql --no-defaults -N -B -e "SELECT CONCAT(@@bind_address,\" | \",@@collation_server,\" | \",@@innodb_buffer_pool_size,\" | \",@@innodb_io_capacity,\"/\",@@innodb_io_capacity_max,\" | redo=\",@@innodb_redo_log_capacity)"'
```

✅ EXPECT exactly:
`0.0.0.0 | utf8mb4_0900_ai_ci | 805306368 | 1000/2000 | redo=268435456`

⚠ `redo=104857600` (100M) means Oracle's packaging dropped
`/etc/mysql/mysql.conf.d` — the config is not applied. `bind_address` of
`127.0.0.1` is the same failure and is the one that breaks App Platform while
your local client keeps working.
⚠ `innodb_doublewrite_pages` will read **128**, not 4. That is correct and
deliberate — it is not pinned.

### 4.2 Auth over the transport the app actually uses

```bash
$SSHQ 'mysql --no-defaults -N -B -e "SELECT user,host,plugin FROM mysql.user WHERE plugin<>\"caching_sha2_password\""'
$SSHQ 'test -f /etc/mysql/debian.cnf && echo "debian.cnf present" || echo "debian.cnf GONE - logrotate will fail silently, file a ticket"'
```

✅ EXPECT: only `root@localhost auth_socket`.

⚠ A plain `mysql -h 127.0.0.1` negotiates TLS and never exercises the RSA
public-key path `caching_sha2_password` needs over the no-TLS VPC link. It
passes while production still cannot connect. The real proof is `/ready` below.

### 4.3 Confirm traffic is flowing again

⚠ There is nothing to scale back up — nothing was scaled down (2.1). The
backend reconnects on its own once MySQL answers, because its credentials did
not change in Phase 3. Poll until it does.

```bash
curl -s https://$(doctl apps get $APP_ID --format DefaultIngress --no-header | sed 's|https://||')/ready
curl -s https://$(doctl apps get $APP_ID --format DefaultIngress --no-header | sed 's|https://||')/health/dependencies
```

✅ EXPECT: `/ready` → `database: connected`, and `/health/dependencies` → 200
with both checks `ok`. Then log in through the UI and load one real page.
`/ready` proves database connectivity only; `/health/dependencies` proves the
session store is reachable too, which is what login actually needs.

---

## Phase 5 — Rename `pfv2` → `tbd`  (separate phase, never interleaved)

Run this **only after Phase 4 is green**. Two reasons: the upgrade is
effectively irreversible while the rename is trivially reversible (rename back),
and keeping them sequential means a failure is immediately attributable to one
change.

### 5.1 Re-verify the assumption that makes it cheap

```bash
$SSHQ 'mysql --no-defaults -t -e "SELECT (SELECT COUNT(*) FROM information_schema.views WHERE table_schema=\"pfv2\") views, (SELECT COUNT(*) FROM information_schema.triggers WHERE trigger_schema=\"pfv2\") trgs, (SELECT COUNT(*) FROM information_schema.routines WHERE routine_schema=\"pfv2\") routs, (SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=\"pfv2\" AND table_type=\"BASE TABLE\") tables"'
```

✅ EXPECT: `0 | 0 | 0 | 50`. Views, triggers and routines do **not** move with
`RENAME TABLE`; their absence is what makes this a complete operation rather
than a partial one. A non-zero count means stop and re-plan.

### 5.2 Generate and run the rename — one atomic statement

⚠ There are **83 FK declarations**.

⚠ **The reason is NOT that a table-by-table rename would fail partway.** That
claim stood here for months and is FALSE. Measured on MySQL 8.4.11, production's
exact version, 2026-08-27: renaming a parent out from under its child SUCCEEDS,
and MySQL silently rewrites the child's foreign key to point ACROSS schemas —
`src.child` was left holding a live, enforced FK to `dst.parent` (a bad
reference still raised errno 1452, a good one still inserted). A truncated
rename therefore does not announce itself; it yields a working database wired
across two schemas that breaks later, most obviously when the old schema is
dropped. Silent success is worse than failure, which is why the generator
ASSERTS its pair count. MySQL renames all pairs atomically in a single
statement.

```bash
./infra/ansible/bin/gen-rename-sql.sh --host $DROPLET_IP --from pfv2 --to tbd > /tmp/rename.sql
head -4 /tmp/rename.sql; echo '...'; grep -c '^  pfv2\.' /tmp/rename.sql
```

✅ EXPECT the table count to equal **50**. The generator asserts this itself and
refuses to emit a truncated list.

```bash
$SSHQ 'mysql --no-defaults' < /tmp/rename.sql
$SSHQ 'mysql --no-defaults -N -B -e "SELECT CONCAT(schema_name) FROM information_schema.schemata WHERE schema_name IN (\"pfv2\",\"tbd\")"'
$SSHQ 'mysql --no-defaults -N -B -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=\"tbd\""'
```

✅ EXPECT: `tbd` present, 50 tables. `alembic_version` moves with the rest, so
alembic continues from the same head with no stamping.

### 5.3 Grants and secrets follow the rename

```bash
$SSHQ 'mysql --no-defaults -e "GRANT ALL PRIVILEGES ON tbd.* TO \"pfv_app\"@\"%\"; GRANT ALL PRIVILEGES ON tbd.* TO \"pfv_app\"@\"localhost\"; GRANT SELECT, LOCK TABLES, SHOW VIEW, EVENT, TRIGGER, RELOAD, REPLICATION CLIENT ON *.* TO \"pfv_backup\"@\"localhost\"; FLUSH PRIVILEGES;"'
```

⚠⚠ Then update **both** `DATABASE_URL` bindings again — the database name is in
the URL. Service **and** migrate job. Missing the job's copy fails at the next
deploy, not now.

```bash
$SSHQ 'grep -n "pfv2" /usr/local/bin/mysql-backup.sh 2>/dev/null || echo "  backup script: no pfv2 reference"'
```

⚠ If the nightly backup script names `pfv2`, it silently starts dumping nothing.
Update it, or re-run the play once `mysql_app_db` is changed to `tbd`.

🔙 **ROLLBACK:** rename back. `RENAME TABLE tbd.x TO pfv2.x, ...` and restore
the previous `DATABASE_URL`s.

---

## Phase 6 — Close out

```bash
# 1. Prove the nightly backup still works, by hand
$SSHQ '/usr/local/bin/mysql-backup.sh && ls -lh /var/backups/mysql/ | tail -3'

# 2. Confirm the migrate job can still reach the DB (it has its OWN DATABASE_URL)
doctl apps create-deployment $APP_ID --wait
doctl apps logs $APP_ID migrate --type run | tail -5
```

✅ EXPECT a non-empty dump, and `migrate.no_op` (or `migrate.complete`) naming
the **new** database.

⚠ The nightly backup has four known holes — on-disk only, one database only so
**grants are not backed up**, `gzip >` creates the file before `mysqldump` can
fail so existence ≠ success, and no alerting. Tracked as **TBD-400**.

### Then, as separate PRs

| Change | Where |
|---|---|
| `enable_backups = true` → `false` | `infra/terraform/main.tf` (TBD-399's stated revert) — **already prepared, see the open PR** |
| ~~"production still on 8.0"~~ — done 2026-08-19 | `README.md`, `CLAUDE.md` |
| ~~Add production as the final evidence row~~ — done 2026-08-20 | `MYSQL-84-CUTOVER.md` |
| ~~Decide whether CI keeps the `mysql: ["8.0","8.4"]` matrix~~ — decided 2026-08-20 (TBD-415): dropped to `["8.4"]`, matrix shape kept so the job name is unchanged | `.github/workflows/test.yml` |
| ~~Fill in the outcome record~~ — done 2026-08-19 | `specs/2026-08-18-mysql-84-cutover-record.md` |
| ~~⚠ Still open: the matrix and the status notes~~ — all cleared 2026-08-20 (TBD-415), including `infra/ansible/README.md` and `infra/ansible/bin/run-playbook.sh`, which this row omitted | see the record's follow-ups |

```bash
# Destroy the pre-cutover snapshot once you are confident (it holds a full copy
# of production). Keep it at least a few days.
doctl compute snapshot delete <snapshot-id> --force
```

---

## If it goes wrong

| Symptom | Meaning | Action |
|---|---|---|
| App cannot authenticate after Phase 1 | `DATABASE_URL` not updated, or updated on only one of the two bindings | Fix the secret; no rollback needed |
| `E: Unable to locate package mysql-community-server` | Expired signing key, **not** a wrong component name | Use `mysql-apt-config`, never the standalone `-2023` key |
| `ERROR 1227 ... SUPER` | You dropped `--no-defaults`; you are `pfv_backup` | Re-run with `--no-defaults` |
| 8.4 will not start | `default-authentication-plugin` still in a config file | `grep -rn` it out, restart |
| `redo=104857600` / `bind_address=127.0.0.1` after cutover | `/etc/mysql/mysql.conf.d` no longer included | Re-add `!includedir` to `/etc/mysql/my.cnf`, restart |
| Anything worse | — | `doctl compute droplet-action rebuild $DROPLET_ID --image <snapshot-id> --wait` |

⚠ After **any** rebuild/restore, the accounts are back on
`mysql_native_password` and the secrets are back to the old values. Re-run
Phase 1 before trying again.

**In-8.4 escape hatch:** if an unconverted account is discovered mid-window,
`mysql_native_password` can be re-enabled on 8.4 with
`--mysql-native-password=ON`. It is removed entirely in 9.0, so it buys time,
not a resting place.
