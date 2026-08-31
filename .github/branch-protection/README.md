# `main.json` — what this file is, and what it is not

`main.json` is the **measured** normalized state of `main`'s classic branch
protection, recorded so that a later reading can be classified as drift or as
intent. Before it existed, `main`'s protection had no recorded intended state at
all, so no reading of it could be classified as either — which is how
`allow_force_pushes` sat drifted for at least three weeks, unnoticed, after
`allow_deletions` had already drifted six days earlier.

## It is measured, not ratified

**Nobody chose these values. They are what the repository has.** Two of them in
particular are recorded because they are true, not because they are correct:

| field | recorded | note |
|---|---|---|
| `allow_force_pushes` | `true` | **Not ratified.** `reference_branch_protection_and_breakglass` recorded the intent as `false`. Flipping it is an operator decision and a one-line diff to this file. |
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
  on `GET /repos/{owner}/{repo}`, **not** on `/protection`. `CLAUDE.md` asserts
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
