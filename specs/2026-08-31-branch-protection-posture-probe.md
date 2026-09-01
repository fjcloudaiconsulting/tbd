# Branch-protection posture probe (TBD-420)

Revision 3, 2026-08-31. Revisions 1 and 2 were both **REJECTED** by adversarial
review (13 and 9 blocking findings), and a build-it round implemented the kernel
and ran the fences against both.

⚠ **Eight of revision 2's nine blocking findings were in material revision 2
added.** Three consecutive rounds found mechanics in the same three places — the
re-read, the rulesets guard, `_unratified` — while the core survived every round
untouched. **Revision 3 is therefore mostly deletion.** The ruling has never
changed; the accreted mechanism around it has been removed.

## Why the ticket's framing had to be replaced

**Measured on `main`, 2026-08-31, from the full payload rather than a projection:**

| field | live | recorded intent |
|---|---|---|
| `enforce_admins` | `true` | `true` (`reference_branch_protection_and_breakglass:23`) |
| `allow_deletions` | `false` | `false` (`:24`) |
| **`allow_force_pushes`** | **`true`** | **`false` (`:25`)** |

`allow_force_pushes` has been drifted for at least three weeks unnoticed,
undocumented in `CLAUDE.md`, unmentioned in the ticket. It is the *second* field
to drift after the `allow_deletions: true` incident `scripts/breakglass-merge.sh`
cites.

⚠ **The ticket's own evidence of health is the defect it describes.** Its
grooming comment pastes a four-field projection — `{checks, enforce_admins,
reviews, strict}` — and concludes "DoD 1 is already satisfied". That projection
is exactly what hid `allow_force_pushes`.

The real finding: **`main`'s protection has never had a recorded intended state,
so no reading of it can be classified as drift or as intent.**

## Ruling: observe STATE, do not instrument the OPERATION

**(B) — breakglass writes a durable record — is structurally blind.**
`scripts/breakglass-merge.sh` only touches `.../protection/enforce_admins`
(DELETE `:57`, POST `:35`). It never PUTs `/protection`, so it **cannot** have
caused `allow_deletions: true` or the live `allow_force_pushes: true`.

⚠ The better-evidenced unenumerated actor is in memory: *"the protection PUT is a
FULL REPLACE. Every omitted field resets to default. This is almost certainly how
`allow_deletions: true` got set unnoticed."* Instrumenting one script cannot see a
full-replace PUT or a console click.

⚠ (B)'s premise is *measurably* spent: `breakglass-merge.sh:38` already prints
`!!! FAILED TO RE-ARM enforce_admins`. It printed and nobody read it.

⚠ The trap is **not** defeated by closing the terminal — bash runs the EXIT trap
on an untrapped SIGHUP, then exits 129. `breakglass-merge.sh:20-21` claims it
"re-arms on EVERY exit path"; that is false and must be corrected. The real
windows are `SIGKILL`/power loss and the re-arm POST returning non-zero.

**(C) is the status quo with a name.** Two accidental discoveries six days apart,
and between them TBD-404 was designed against the false premise that the gate was
armed.

## Coverage boundary — what this does NOT cover

The promise is exactly:

> **The normalized fields of `main`'s CLASSIC branch protection equal a committed
> file.**

Not covered, and each must be said aloud in `CLAUDE.md` and the workflow header:

* **`allow_merge_commit` / `allow_rebase_merge` / `allow_squash_merge`** — these
  live on `GET /repos/{o}/{r}`, **not** on `/protection`. `CLAUDE.md` asserts
  squash-merge-only three bullets above line 331, and the release pipeline
  depends on it: the squash subject *is* the string semantic-release parses.
  Re-enabling merge commits leaves this probe green. **The most important
  uncovered setting in the repo.**
* Repository and org-level rulesets.
* Org-level policy; required-workflow configuration.
* Any field whose key ends `_url` (stripped during normalization).
* Whether a red required check *actually blocks a merge* — never once observed.

A monitor that overstates its coverage is worse than none, because the next
TBD-404 will cite it.

## Design

### The posture file — `.github/branch-protection/main.json`

The normalizer's output, verbatim, seeded from measured live state. Compared
strict-equal in **both directions**.

⚠ **Seeded from measurement, not aspiration.** An aspirational posture
(`allow_force_pushes: false`) makes the probe red on the day it merges, and a
monitor red from birth is trained into noise before it is trusted.

⚠ **A key live but absent from the file is DRIFT; a key in the file but absent
live is DRIFT.** Never project-and-ignore.

⚠ **No count of its keys appears anywhere.** Revision 1 said "sixteen fields",
revision 2 corrected that to "11 top-level keys" — a different magic number, in
the paragraph explaining why the first one was harmful. A future reader fences
against whatever number is written down.

**Regeneration is the normalizer itself** — no separate generator script:

```
gh api repos/:owner/:repo/branches/main/protection \
  | python3 scripts/ci/normalize_protection.py > .github/branch-protection/main.json
```

⚠ Revision 1 banned all regeneration and **inverted the precedent it cited**:
`report-sources.json` and `.test_durations` both have documented generators. What
those precedents ban is *self-regeneration inside the failing check*. Revision 2
then added a **separate** generator script, which (a) could drift from the
normalizer the checker uses and (b) silently ate the `_unratified` annotation on
every legitimate regeneration. Using the one normalizer for both removes both
problems and keeps the "nothing writes the posture path" fence absolute.

**`_unratified` is SUBTRACTED** — see Subtracted, below.

### Normalization — `scripts/ci/normalize_protection.py`

Passes, in this order: **strip** `url` / `*_url` → **collapse** → **reduce
members** → **sort every list**.

⚠ **The collapse fires only on the exact key set `{enabled}`**, never on
`"enabled" in d`. A loose collapse returns the bool and **silently discards every
sibling key**, reintroducing the projection blindness the both-directions rule
exists to kill: `{"url":…,"enabled":true,"some_new_toggle":true}` would make
`some_new_toggle` invisible forever.

⚠ **Strip-before-collapse matters only under an exact-key-set collapse**, which
revision 1 never specified — so its single F17 *passed against the bug it named*.
Both properties are fenced separately (F17a, F17b).

⚠ **Sort key is `json.dumps(x, sort_keys=True)`, and this is not a detail.**
`required_status_checks.checks` is a list of **objects**
(`[{context, app_id}, …]`, proven in-repo at `breakglass-merge.sh:51`). Revision 2
said "sort every list" and named no key; measured, a bare
`sorted([{...},{...}])` raises `TypeError: '<' not supported between instances of
'dict' and 'dict'` — the checker dies and the probe is in **permanent
`could-not-run` from its first green-path run**. The realistic response to that
TypeError is to skip sorting `checks`, which reintroduces the false `drifted`
that sorting exists to prevent, with F18 still green because it will have been
written against `contexts` (a list of strings).

⚠ **Members reduce to `{id, login|slug|name}`, not to a bare name.** Revision 1
said "sorted logins" and was wrong on fact — **team and app objects carry `slug`,
not `login`** — so a literal implementation either raises `KeyError` or maps every
team and app to `None`, and changing `bypass_pull_request_allowances.apps` from
one app to another compares `[None] == [None]` and reports **green**: the promise
false in the one sub-object that grants bypass. Keeping `id` alongside the name
also means a **released-and-reclaimed username** is caught (the id changes) while
a plain rename shows up as an honest one-line posture diff rather than a silent
grant change.

### The verdict script — `scripts/ci/check-branch-protection.sh`

Pure. Live payload on **stdin**, everything else in env (`BRANCH_PROTECTED`,
`EFFECTIVE_RULES_JSON`, `POSTURE_FILE`). Exits 0 `in-posture` / 1 `drifted` / 2
`could-not-run`.

⚠ **stdin, not "the script calls `gh`"** — per `check-backup-freshness.sh`'s
header: a probe exercisable only against healthy live state proves nothing about
its unhealthy paths, and those are the only paths that matter.

**Decision order:**

1. **`BRANCH_PROTECTED == "false"` → `drifted`, "`main` is not protected at
   all".** First, so that an unparseable posture file cannot mask it.
2. posture file missing or unparseable → `could-not-run`
3. stdin empty, unparseable, **or sharing zero keys with the posture** →
   `could-not-run`. The shape guard is required: a 200 carrying
   `{"message":"Not Found"}` *parses fine* and would otherwise compare as
   `drifted`. One overlapping key is enough to proceed.
4. compare normalized live vs posture, both directions → `drifted` / `in-posture`

⚠⚠ **The two reads MUST use different credentials, and step 1 is worthless
otherwise.** Revision 2 moved `.protected` to step 1 claiming it is
"independently sourced", while specifying a single `${GH:-gh}` credential — so a
suspended App made *every* call fail, `BRANCH_PROTECTED` unknown, and a genuinely
naked `main` reported `could-not-run`: the exact fail-open the reorder was
performed to prevent, invisible to F9 because F9 drives the checker from an env
var. Therefore:

* `GET /repos/{o}/{r}/branches/main` (for `.protected` only) → `${{ github.token }}`,
  public repo metadata, needs only `contents: read`.
* `GET /repos/{o}/{r}/branches/main/protection` → the **App** token.
* `gh issue` in the notifier → `${{ github.token }}`. The App holds
  `Administration: read` and **cannot** open issues; pointing `GH_TOKEN` at it for
  the whole job makes the notifier 403 and exit 1 — drift detected, no issue
  opened, red square nobody opens.

**`BRANCH_PROTECTED` is TRI-STATE**: `true` / `false` / unknown. The guard is
`== "false"` and **never `!= "true"`** — the latter reads as an equivalent
refactor while turning "our own fetch flaked" into a drift alarm.

⚠ **`.protected` is not a classic-protection bit** — it reads `true` under a
ruleset too. It is a one-bit "is anything protecting this branch" oracle, never a
source for posture fields. In particular the code must never read
`.protection.required_status_checks.enforcement_level`, which *is*
`enforce_admins` and would turn a disambiguator into a partial source.

⚠ **Status-code splitting cannot substitute for it.** 404 is what an unprotected
branch returns *and* what a token lacking permission returns — and for a **GitHub
App** a missing permission commonly surfaces as `403 Resource not accessible by
integration`. (Revision 1 asserted "404, not 403" categorically; that is PAT
behaviour and was overstated.) The status code does not reliably distinguish the
cases either way.

### Rulesets: one arm only, and it says "retire this probe"

`EFFECTIVE_RULES_JSON` comes from **`rules/branches/main`** — the *effective* view
including **org-level** rules; `rulesets` lists repo-level only and is blind to
exactly the case worth seeing. It is **never a data source**: both return `[]`
here, so a probe reading its verdict from them reports "no problems" forever while
classic `enforce_admins` sits disarmed.

* `/protection` **readable** → compare as normal. A ruleset appearing alongside
  classic protection is **additive** and changes nothing about the comparison.
* `/protection` **unreadable** + `.protected == true` + rules non-empty →
  `could-not-run`, and the message says **"classic protection is gone; `main` is
  now governed by rulesets, which this probe cannot read. Retire or replace it."**

⚠ Revision 1 made *any* non-empty rules a `could-not-run`, which turned the
operator legitimately *hardening* the repo into a permanent alarm on every push.
Revision 2 demoted it to an "informational line" that had **no delivery path** —
written to the stdout of a green job that notifies nobody, which is worse than a
red square. Revision 3 drops the informational line entirely (the coverage
boundary carries the disclosure) and keeps one arm.

⚠ That surviving arm **is** a persistent alarm on the ruleset-*migration* path —
GitHub's own "migrate to rulesets" affordance removes the classic rule, so
`/protection` 404s while `.protected` stays `true`. That is **correct and
intended**: the probe has become unable to answer its question, and the alarm
says so and names the remedy. It is not a false alarm; it is a true statement
that the floor's representation changed and this artifact needs replacing.

### The probe script — `scripts/ci/probe-branch-protection.sh`

Fetches with the credential split above, then runs the checker. `${GH:-gh}`
indirection so every path is fixture-drivable without a credential.

**It reads ONCE and reports once. The re-read is SUBTRACTED** — see below.

### The workflow — `.github/workflows/branch-protection-probe.yml`

```yaml
on:
  push:      { branches: [main] }   # PRIMARY. No paths filter. FENCED, not commented.
  schedule:  [ { cron: "41 5 * * *" } ]
  workflow_dispatch:
concurrency:
  group: branch-protection-probe
  cancel-in-progress: false
```

**Unfiltered `push: main` is the primary trigger.** The suspected primary cause
produces a push to `main`, collapsing detection from 9 days to about a minute.
Public-repo scheduled workflows are **auto-disabled after 60 days of inactivity**,
so the cron is the backstop, not the mechanism. Safe because the probe reports no
commit status and can never block a PR.

⚠ Revision 1 guarded this with a YAML comment only, in a repo whose standing rule
is *a grep can be satisfied by a comment*. Now fenced (F7).

**`cancel-in-progress: false`.** A run does not end at the fetch, it ends at the
`gh issue` write; cancellation between those points destroys an already-earned
alarm. Precedent across `.github/workflows/` is 3 `false`
(`release.yml:81`, `apex-deploy.yml:147`, `backup-freshness-probe.yml:32`), 2
`true` (`deploy-drift-probe.yml:46`, `test-durations.yml:53`), 1 conditional
(`test.yml:59`), and `release.yml:75-78` already carries this mechanism argument.

⚠ The comment must name the **distinguishing property**, not the conclusion: this
probe reads global state and produces no per-run answer; if it ever gains a
per-run input, the documented `cancel-in-progress` trap re-inverts.

**Credential: a GitHub App (`Administration: read`).** `GITHUB_TOKEN` structurally
cannot read protection — no `administration` scope in the workflow `permissions:`
key. A fine-grained PAT works but **dies on a calendar date**, and a recurring
manual chore in a solo-maintainer repo is what decays.

⚠ **The auth step is `continue-on-error: true`** (per
`backup-freshness-probe.yml:48-64`) so a revoked App becomes a loud
`could-not-run`, not a red square.

⚠⚠ **Sequencing: the App and its secrets must exist BEFORE this merges.** This
probe fires on **every push to `main`, unfiltered**; merging credential-less
would spend the alarm's whole credibility on unactionable noise before it ever
reports something true. (Revision 1 cited `backup-freshness-probe` as precedent
for shipping first — that probe fires once daily on a paths-filtered push.)

Job-level `permissions: contents: read` + `issues: write` — job-level **replaces**
workflow-level.

Alarm and job failure both gate on `verdict != 'in-posture'`, never `== 'drifted'`.

### Notifier — `scripts/notify-protection-drift.sh`

Own dedupe bucket `[branch-protection]`, fenced (F8). No auto-close.

⚠ This is the **fifth** copy of the `gh issue` plumbing. `notify-undeployed-release.sh`
said extract "on the third"; `notify-deploy-drift.sh:11-19` declined at the third.
Declining at the **fifth** needs its own argument, and it is: the extraction would
make a reviewer assessing a new probe silently review a refactor of four live
incident notifiers whose failure mode is silence during an outage. **File the
extraction** — the next probe should not add a sixth.

⚠ The house has **two** notifier idioms, and F8 must know it: only
`notify-deploy-drift.sh:29` and `notify-backup-stale.sh:15` define `TITLE_PREFIX`.
`notify-smoke-failure.sh:42` and `notify-undeployed-release.sh:73` hardcode
`TITLE` and a separate `--search '"[…]" in:title'` literal. A fence collecting
`TITLE_PREFIX=` assignments finds three values, asserts they differ, passes — and
a new notifier copied from the *other* idiom with `[smoke-fail]` left in place
deduplicates into the smoke-failure issue, producing zero signal during an
incident. **F8 asserts over the dedupe literals actually used in the
`gh issue list --search` calls.**

### Also in scope

* `CLAUDE.md:331` — gains the probe, the **coverage boundary** verbatim, and the
  unratified `allow_force_pushes`.
* `scripts/breakglass-merge.sh:20-21` — correct the false "re-arms on EVERY exit
  path" claim.
* `reference_branch_protection_and_breakglass.md:25` records
  `allow_force_pushes: false` and will contradict the posture file. Update it, or
  the next agent believes whichever it reads first.
* `.github/branch-protection/README.md` — one paragraph: this file is measured,
  not ratified; `allow_force_pushes: true` and `allow_deletions: false` are
  recorded because they are what the repo *has*, not because anyone chose them.

⚠ **Do NOT touch `scripts/ci/detect-changed-areas.sh`.** Revision 2 mandated
classifying `.github/branch-protection/`, citing the `infra/MIGRATION.md`
exception. Measured: a `.json` there already falls to the `*)` catch-all
(`detect-changed-areas.sh:126`) → `backend=true frontend=true migrations=true`,
and that is already fenced by
`test_ci_change_detection.py::test_an_unclassified_path_runs_everything`. The
`MIGRATION.md` exception exists only to beat the inert `*.md` case, which a JSON
file does not have. Adding a rule above `*.md` would **narrow** the
classification. A mandated edit to a load-bearing CI classifier whose only
possible effect is weakening.

## Subtracted in revision 3

Every item here was found defective by review, and deletion was chosen over
correction because three rounds kept finding mechanics in the same places.

1. **The re-read, its severity table, and fences F12/F13.** The table's first row
   (`drifted → in-posture ⇒ in-posture`) encoded *"observed drift, zero alarms"* —
   the exact pattern this spec declares fatal one page earlier to justify
   `cancel-in-progress: false` — and F13 **fenced the defect in**, so a future
   agent who fixed it would go red and revert. It also made the probe blind by
   construction to the one event it is uniquely placed to see: a break-glass
   disarm has no other automated trace in this repo. The race it existed for was
   never measured, and the failure actually feared (a re-arm POST returning
   non-zero) is *persistent* and unaffected by re-reading. **Read once, report
   once.** A rare stale alarm is absorbed by the title-prefix dedupe.
2. **`_unratified`, its self-validation, its collision guard, and F19.** It was
   mechanically inert — nothing depended on the list's contents beyond "these are
   real keys of this file", so listing all of them passes. It reserved a
   namespace in a public repo, and the generator revision 2 added destroyed it on
   the first legitimate regeneration. Its job is done by
   `.github/branch-protection/README.md`, where no generator can eat it.
3. **The separate generator script.** The normalizer is the generator.
4. **The rulesets informational line** — no delivery path.
5. **The `detect-changed-areas.sh` instruction** — already true; editing can only
   narrow.
6. **The `--noconftest` module constraint.** It was unsatisfiable (F7/F14/F15/F21
   need `yaml`, which is not stdlib, and `test_deploy_drift_probe.py:17` imports
   it) *and* it misdiagnosed its own evidence: `structlog` is imported at
   `conftest.py:408` **inside an autouse fixture**, which runs for every test in
   the package, so a stdlib-only module errors identically. Replaced by a process
   note: **read the pytest summary line — 49 errors is not 49 passes.**
7. **Any count of the posture file's keys.**
8. Auto-remediation; the PAT-expiry header read; a `CLAUDE.md` copy of the posture
   table; auto-closing the issue; `fetch-depth: 0`; a watcher-of-the-watcher.

## Tests

`backend/tests/test_branch_protection_probe.py`, riding the existing shards. Every
entry names the wrong implementation it kills.

⚠ **Read the pytest summary line.** The build-it round's first "RED" run reported
49 errors that were all `ModuleNotFoundError` from `conftest.py`'s autouse
fixture: **the fences had not run at all.** Trusting it would have reported a
green fence set containing a dead fence.

| # | kills | wrong implementation |
|---|---|---|
| F2 | unknown live key | live carries a key absent from posture → `drifted`, not ignored |
| F3 | missing live key | posture names a key absent live (incl. `enforce_admins` vanishing) → `drifted`. Naive dict-intersection returns equal |
| F4 | value drift | `enforce_admins: false` → `drifted` |
| F4b | member reduction | an unrelated profile edit (avatar URL) on a bypass user must **not** be drift |
| F4c | member identity | `bypass_pull_request_allowances.apps` changing from one app to another → `drifted`. Kills `x.get("login")` → `[None]==[None]`. Drive **teams and apps**, not only users |
| F5 | could-not-run as all-clear | empty / whitespace / `{}` / non-JSON → exit 2. **Plus the shape guard**: a 200 body sharing zero keys with the posture → exit 2, not `drifted` |
| F6 | alarm gated on drift | `if: verdict == 'drifted'` is silent on `could-not-run`. **Parse** the expression, normalize whitespace and the `${{ }}` wrapper; assert it on **both** the notify step and the fail step, by step id |
| F7 | filtered push trigger | `push.branches == ["main"]` **and** no `paths` **and** no `paths-ignore`, plus the schedule exists. Kills copying a sibling probe's `on:` block |
| F8 | shared dedupe bucket | the dedupe literals **actually used in the `gh issue list --search` calls** across all five notifiers are pairwise distinct. ⚠ Not `TITLE_PREFIX=` assignments — two of the four existing notifiers do not have one |
| F9 | naked main as unwell | `.protected == false` → `drifted` **even when the posture file is unreadable** |
| F9b | tri-state `.protected` | unknown (empty) → `could-not-run`, not `drifted`. Kills `!= "true"` |
| F9c | credential split | the `.protected` fetch uses `github.token` and the `/protection` fetch uses the App token; a failed App auth must still yield `drifted` on a naked `main`. ⚠ Behavioural through the probe script — F9 drives the checker from an env var and structurally cannot see this |
| F10 | rules as source | `EFFECTIVE_RULES_JSON` containing a copy of the posture must **not** rescue a drifted verdict |
| F10b | rules as premise guard | rules non-empty + `/protection` readable → still compares, **not** `could-not-run` |
| F11 | probe mutates | any `-X`/`--method` `PUT/POST/DELETE/PATCH`, **and** any `-f`/`-F`/`--field`/`--raw-field`/`--input` co-occurring with a `branches/*/protection` path — `gh api` defaults to **POST** whenever a field flag is present, so `-X`-only matching has a live bypass. ⚠ Scope to the three new files by name; `breakglass-merge.sh:35,57` legitimately POST/DELETE that path |
| F14 | concurrency | the parsed boolean `False`, not the raw text |
| F15 | job-level perms | `issues: write` on the **job**, not the workflow |
| F16 | self-regenerating posture | ⚠ **measured vacuous in revision 1** — it passed with the whole implementation deleted, because a search fence with no positive anchor cannot tell "nothing writes the file" from "nothing was searched". Needs a **named corpus** (`scripts/ci/*`, `scripts/notify-*`, the workflow) asserted non-empty, plus an `open(…,"w")` / `--update`-flag half: a real regenerator mutant was **not** caught by the path scan, since the offending line never spells `branch-protection` |
| F17a | loose collapse | collapse fires only on the exact key set `{enabled}`; `{"enabled":true,"some_new_toggle":true}` survives as a dict |
| F17b | collapse before strip | `{"url":…,"enabled":true}` → `true`. ⚠ Only meaningful **given F17a** |
| F18 | list ordering | `checks` (a list of **objects**) and `contexts` returned in a different order → `in-posture`, not a false `drifted`. ⚠ Must exercise `checks`, not only `contexts` — a bare `sorted()` on the object list raises `TypeError` and a fence written against `contexts` alone stays green |
| F20 | fetch-layer projection | ⚠ the highest-value fence. Every other fence drives the checker from stdin, so the projection defect is reintroducible one layer up with all of them green: `gh api … --jq '{enforce_admins, …}'` with a posture seeded from the same command compares four keys and reports `in-posture` forever. Assert (i) the **protection** fetch carries no `--jq`/`-q`/`--template` projection — exempting the `.protected` fetch, which legitimately needs one — and (ii) behaviourally, a canary key injected into a stubbed `${GH:-gh}` response survives the probe's pipeline into the checker's stdin |
| F21 | non-vacuity baseline | workflow parses, `probe` job exists, ≥5 steps parse. Without it a job rename empties every collection and F6-F15 pass asserting nothing. ⚠ `yaml.safe_load` parses `on:` as boolean `True` — read both spellings |

## Filed separately, not built here

* `CLAUDE.md:331` claims a red required check blocks a merge and **nobody has ever
  observed it**. `enforce_admins: true` is one necessary condition; the same
  payload grants `bypass_pull_request_allowances` to both maintainer accounts.
  Remedy: one throwaway PR carrying a deliberately failing test.
* Extraction of the now-fivefold `gh issue` notifier plumbing.
* Ratifying `allow_force_pushes` — an operator decision, a one-line posture diff.
