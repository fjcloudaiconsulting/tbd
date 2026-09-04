# `main.json` — what this file is, and what it is not

`main.json` is the **measured** normalized state of `main`'s classic branch
protection, recorded so that a later reading can be classified as drift or as
intent. Before it existed, `main`'s protection had no recorded intended state at
all, so no reading of it could be classified as either — which is how
`allow_force_pushes` sat drifted for at least three weeks, unnoticed, after
`allow_deletions` had already drifted six days earlier.

## It is measured, not ratified

**These values are measured from the live API, never hand-written.** Most are
recorded because they are true rather than because anyone chose them; the two
below have been looked at directly:

| field | recorded | note |
|---|---|---|
| `allow_force_pushes` | `false` | **Ratified 2026-09-01.** It had drifted to `true` for at least three weeks; the operator ruled it back to `false` and the setting was restored before this file recorded it. Nothing in this repo pushes to `main` — semantic-release tags but never commits back. |
| `allow_deletions` | `false` | Matches the recorded intent. Listed here only because it is the other field that has drifted before. |

Seeding this file with the *aspirational* values instead would make the probe red
on the day it merged, and a monitor that is red from birth is trained into noise
before it is ever trusted. Green therefore means exactly **"nothing has changed
since a human last looked"** — a claim the probe can actually make — and never
"the posture is correct", which is not mechanizable.

## What the probe covers

> The normalized fields of `main`'s **classic** branch protection equal this file.

**Not covered.** Each of these can change with this probe staying green:

* `allow_merge_commit` / `allow_rebase_merge` / `allow_squash_merge` — these live
  on `GET /repos/{owner}/{repo}`, **not** on `/protection`. `CONTRIBUTING.md` asserts
  squash-merge-only and the release pipeline depends on it, because the squash
  subject *is* the string semantic-release parses. **This is the most important
  uncovered setting in the repo.**
* Repository and organization rulesets.
* Organization-level policy and required-workflow configuration.
* Any field whose key ends in `_url` (stripped during normalization).
* Whether a red required check *actually blocks a merge* — never once observed.

A monitor that overstates its coverage is worse than none, because the next
design decision will cite it.

## Regenerating it

There is no generator script. The normalizer the checker itself uses is the
generator, so the two can never disagree:

```
gh api repos/:owner/:repo/branches/main/protection \
  | python3 scripts/ci/normalize_protection.py > .github/branch-protection/main.json
```

Nothing regenerates this file automatically, and nothing may. **The diff hunk is
the evidence that a human looked** — the same posture `report-sources.json` and
`.test_durations` already pay for. A check that repairs its own expectation can
never be red, and therefore detects nothing.

The committed file is a byte-identical round-trip of live state through that
command, verified against the API:

```
gh api …/protection | python3 scripts/ci/normalize_protection.py \
  | diff - .github/branch-protection/main.json      # IDENTICAL
```

## First-time setup: the probe's GitHub App

⚠⚠ **Do this BEFORE the probe merges.** It fires on every push to `main`,
unfiltered, so a credential-less merge alarms `could-not-run` once per merge
forever — spending the alarm's credibility before it ever reports something
true. This is written down because it is a hand-created, out-of-band step that a
future reader cannot infer from the checkout, the same reason
`infra/aws/bootstrap/` exists.

**Why an App and not the workflow token or a PAT.** `GITHUB_TOKEN` structurally
cannot read branch protection — the workflow `permissions:` key has no
`administration` scope. A fine-grained PAT *can*, but it expires on a calendar
date, and a recurring manual chore on a solo-maintainer repo is exactly what
decays. That is the same argument that rejects "just check it manually", so
using a PAT here would apply it inconsistently.

### 1. Create the App

GitHub → **Settings → Developer settings → GitHub Apps → New GitHub App**.

| field | value |
|---|---|
| Name | anything unique, e.g. `tbd-branch-protection-probe` |
| Homepage URL | the repo URL is fine |
| Webhook | **uncheck Active** — the probe polls, it receives nothing |
| Repository permissions | **Administration: Read-only**, and nothing else |
| Where can this be installed | **Only on this account** |

⚠ `Administration: Read-only` is the entire permission set. It cannot merge,
push, read code, or touch production. Do not widen it "to make debugging
easier" — the probe never needs write, and the whole design rests on it being
unable to repair what it observes.

### 2. Install it on this repo only

App page → **Install App** → your account → **Only select repositories** →
`tbd`. Note the **App ID** from the App's settings page, and
**Generate a private key** (downloads a `.pem`).

### 3. Give the workflow its two inputs

```bash
gh variable set PROTECTION_PROBE_APP_ID --body "<the App ID>"
gh secret   set PROTECTION_PROBE_APP_KEY < ~/Downloads/<app-name>.private-key.pem
```

The App ID is a **variable** (not secret — it is not sensitive and a variable is
readable in logs, which helps when the mint fails). The private key is a
**secret**. Delete the local `.pem` afterwards; you can always generate another.

### 4. Verify, and do not skip this

```bash
gh workflow run branch-protection-probe.yml
gh run watch
```

Expect `in-posture` and a green run. A `could-not-run` verdict means the App ID,
the key, or the installation is wrong — **that is the probe working**, not a
bug: it refuses to report health it cannot verify. Fix the credential and re-run
until it is green, because an alarm nobody trusts is worse than no alarm.

### Rotating or replacing the key

Generate a new key on the App page, `gh secret set PROTECTION_PROBE_APP_KEY`
again, then re-run step 4. There is no expiry to track — that is the point of an
App over a PAT — but a revoked or rotated-away key surfaces as `could-not-run`
within one push to `main`, never as a silent green.
