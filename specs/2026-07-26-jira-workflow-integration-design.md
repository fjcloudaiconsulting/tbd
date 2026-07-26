# Jira Workflow Integration Design

**Date:** 2026-07-26
**Jira:** TBD-166
**Status:** approved, first pass in progress
**Scope:** Stand up Jira project TBD as the shared visibility layer over the roadmap backlog, and wire GitHub so issue state follows code activity without manual bookkeeping.

---

## 1. Why

The backlog has lived in two places: `project_roadmap.md` (agent memory, prose + decisions) and `~/src/tbd/specs/` (design docs). Neither is visible to anyone but the operator and the agent holding that memory in context, and the roadmap demonstrably drifts. The 2026-07-16 reconciliation sweep found three "todo" items that had already shipped.

Jira adds three things the current setup cannot provide:

1. **External visibility** into what is queued, in flight, and blocked.
2. **Automatic state** driven by real code events rather than an agent remembering to update a memory file.
3. **A pickup surface** for future autonomous agents, which can query "what is ready to build" instead of parsing prose.

It also adds a second write target, which is a real cost. Section 8 addresses how that cost is contained.

## 2. Environment as found

Verified 2026-07-26 against the live site, not assumed:

| Property | Value |
|---|---|
| Site | `fjconsulting.atlassian.net`, cloudId `77d2c7b6-82b2-45bc-96e5-a61403474bae` |
| Project | `TBD` / "The Better Decision", id `10003` |
| Project type | `software`, **company-managed** (`simplified: false`, style `classic`) |
| Issue types | Epic (10000), Story (10012), Task (10013), Sub-task (10014), Bug (10015) |
| Statuses | **To Do** (10008), **In Progress** (3), **Done** (10009) |
| Transitions | 11 to To Do, 21 to In Progress, 31 to Done. All `isGlobal: true`, so any-to-any is permitted |
| Issue numbering | Starts at TBD-153. Earlier keys were consumed by deleted content |
| Story Points | **Absent from the create screen.** Effort cannot be a numeric field |
| Components | None defined, and the MCP connection cannot create them |
| Fix versions | None defined |

**MCP capability boundary.** The Atlassian connection carries `read:jira-work` and `write:jira-work`. That covers issue CRUD, comments, transitions, and issue links. It does **not** cover workflows, statuses, custom fields, components, versions, boards, permission schemes, or automation rules. Everything in that second list is an operator action in a browser. This boundary is the single most important constraint on the design: anything requiring project administration must be specified precisely enough for a human to apply it, because no agent can.

## 3. Project shape

### Epics are roadmap groups

One Epic per roadmap group **that has open work**. Group A (Reports v3 sources) is complete as of #581 and gets no Epic; it reappears only if new report work lands.

| Epic | Group |
|---|---|
| TBD-153 | B: Dashboard and customization surfaces |
| TBD-154 | C: Credit card and financial primitives |
| TBD-155 | D: AI agentic layer (north-star, parked) |
| TBD-156 | E: Auth and security residuals |
| TBD-157 | F: Payments and monetization (parked by policy) |
| TBD-158 | G: Admin and platform |
| TBD-159 | H: Frontend tech debt and SWR |
| TBD-160 | I: Onboarding, help and UX polish |
| TBD-161 | J: Infra and release engineering |
| TBD-162 | K: Testing |
| TBD-163 | L: Operator and browser-only |
| TBD-164 | M: Post-launch program (large, future) |
| TBD-165 | N: Singletons |

Group N exists because the roadmap has a genuine "ungroupable singleton" section. It is deliberately tiny. If it grows past a handful of issues, that is a signal the grouping needs revisiting, not that this Epic needs expanding.

### Children are individual backlog items

Each open roadmap item becomes one issue, typed by nature of the work:

- **Story** for user-visible capability
- **Task** for chores, infra, tooling, operator actions
- **Bug** for defects, including latent ones that only bite at scale

Sub-tasks are **not** created up front. They are for build slices, minted at spec time when the decomposition is actually known. Pre-inventing slices without a spec produces guesses that then have to be deleted.

### Labels carry what has no field

Since there is no Story Points field and no components, labels do the structural work.

| Family | Values | Purpose |
|---|---|---|
| effort | `effort-xs` `effort-s` `effort-m` `effort-l` `effort-xl` | Mirrors the roadmap legend: XS <1h, S 1-4h, M 0.5-1 day, L 1-3 days, XL 1 week+ |
| readiness | `needs-design` `needs-spec` `has-spec` `ready` | The agent-pickup gate. See section 6 |
| state | `state-gated` `state-parked` `state-deferred` `state-trigger-gated` `needs-decision` | Why something is not simply "next" |
| area | `area-backend` `area-frontend` `area-infra` `area-security` `area-ai` `area-reports` `area-dashboard` `area-admin` `area-payments` `area-seo` `area-testing` | Substitutes for components |
| routing | `operator-only` | No agent code comes from this issue, ever |
| group | `group-a` through `group-n` | Redundant with the Epic on purpose. Survives regrouping and lets JQL filter without an Epic join |

### Priority reflects the real queue

**High** for genuinely next-up work. **Medium** as the default. **Low** for parked and deferred clusters (D, F, M, the deferred CC slices). **Lowest** for XS nice-to-haves. **Highest** is reserved for production incidents and is currently unused, which is accurate: nothing is on fire.

### Issue body template

Every issue carries the same shape so a reader, human or agent, needs no other context:

```
{what and why, carried over from the roadmap prose}

**Effort:** M
**Roadmap group:** C - Credit card and financial primitives
**Spec:** specs/2026-05-28-cc-billing-cycle.md   |   none yet - design-first
**State:** open | gated | parked | deferred

**Definition of done**
- ...

**Notes / decisions in force**
- ...

**Local source:** memory/project_roadmap.md -> GROUP C
```

The `Notes / decisions in force` section is load-bearing. It is where architect locks travel with the work, so a future agent cannot innocently undo one. Example: the #38 bar-chart secondary-dimension restriction is deliberately reversed and must not be re-applied.

## 4. GitHub linkage

### How Jira learns about code

The **GitHub for Jira app is display-only.** It renders branches, commits, pull requests and builds in the development panel. It does not transition anything. That is worth stating plainly because it is the most common misconception about the integration, and designing around the assumption that the app transitions issues produces a workflow that silently never moves.

Linkage happens when the issue key appears in specific places. Per Atlassian's documentation, the key must be in:

| Location | Format | What it links |
|---|---|---|
| Branch name | `TBD-166-jira-workflow-integration` | The branch, and the PR raised from it |
| Commit messages | `feat(infra): TBD-166 add jira integration spec` | The commits, **and GitHub Actions runs as Jira "builds"** |
| PR title | `feat(infra): jira workflow integration (TBD-166)` | The PR |
| GitHub comments | `[TBD-166]` in brackets | An ad-hoc reference |

**Commit messages are not optional.** GitHub Actions workflows surface in Jira as builds only when the key appears in the commit messages behind the PR. Branch name alone gets branches and PRs but no CI status. Since "CI green" is an explicit gate in this project's build loop, the key belongs in commit messages.

Neither addition conflicts with existing conventions:

- The PR title release gate parses the leading `type(scope):` prefix. A trailing ` (TBD-166)` is inert to it.
- The local `commit-msg` hook strips AI attribution only. It does not touch issue keys.

### Retention caveat

The Code and Development tabs surface pull requests linked **within the last 30 days**. A long-dormant issue will look bare in that tab even though its development history is intact on the issue itself. Do not read an empty Code tab as "no work happened."

### Smart commits

Smart commits let the commit message itself drive Jira. They cover the half of the lifecycle the **agent** controls, because the agent authors the commit message: no admin configuration is involved and the narrative travels with the code rather than sitting in a side channel.

**Smart commits fire on commits only. They cannot see pull-request events.** By the time a PR is opened the commits are already pushed, so `PR opened → In Review`, `PR merged → Done` and `PR declined → To Do` are not expressible as smart commits and remain automation rules. The two mechanisms are complementary, not alternatives.

**Prerequisite that fails silently.** The committer's email must resolve to **exactly one** Jira user. If it does not, the command is discarded, the commit still succeeds, and nothing appears in Jira. Atlassian only emails a failure notice when it can identify a recipient; otherwise the failure is silent.

This project hit that exact mismatch on setup: commits were authored as `jorge.flamarion@gmail.com` while the Jira account is `flamarion@fjconsulting.io`. Fixed by setting a **repo-local** `user.email` in `~/src/tbd` (global config left alone). An Atlassian account cannot hold a second email address, so aligning git was the only workable direction.

Consequence to watch: the address must also be verified on the GitHub account, or GitHub shows those commits as unattributed.

**Placement: key in the subject, commands in the body.** Commands must not span lines, and Atlassian explicitly says not to put them in PR titles. Keeping commands out of the subject also keeps the conventional-commit subject clean, which matters because it is the release gate and would feed a generated CHANGELOG.

```
feat(reports): TBD-170 add utilization gauge

TBD-170 #comment Spec specs/....md. Chose a gauge over a bar per design review; per-currency, never summed.
```

**Commands used here**

| Command | Use |
|---|---|
| `#comment <text>` | The "why": spec path, decisions taken, review findings folded |
| `#in-progress` | Optional, on the first commit of a branch. Redundant once the branch-created rule exists |

`#time` is skipped: it needs an admin-enabled time-tracking setting and this project does not track time. `#resolve` is avoided because it cannot set the Resolution field; `#done` is the transition to use.

**Transition-name rules.** Only the text before the first space is processed, so a multi-word transition needs hyphens to disambiguate (`#in-review`, `#in-progress`). A transition command targeting a status that does not exist fails silently, so `#in-review` will do nothing until the In Review status is added.

**Squash-merge duplicate comments: a hazard on paper, not in practice here.** GitHub's squash commit body concatenates the original commit messages, so in principle a `#comment` from a branch commit could be reprocessed on the squash commit and post twice.

**Measured on the first real merge (PR 583): it does not happen.** The squash body was left fully intact, containing three `#comment` commands, and TBD-166 ended with exactly four comments, one from MCP and three from the branch commits. No repeats.

The reason is the email rule again. GitHub authors a squash commit as `<id>+<user>@users.noreply.github.com`, with committer `GitHub <noreply@github.com>`. Neither resolves to a Jira user, so every smart-commit command in a squash body is discarded. The same requirement that silently broke the first smart commit here also suppresses squash duplication.

Treat this as **incidental protection, not a guarantee**: it depends on a GitHub default that is not ours to control, and an account configured to expose its real commit email would bring the duplicates back. There is no need to clear the squash body as a routine step. Build and dev-panel linkage is unaffected regardless, since those match on the issue key rather than the author.

### The `#` collision, verified the hard way

**Any `#word` token terminates the preceding `#comment` and is itself parsed as a command.** This was confirmed empirically on the first live smart commit in this repo: the comment text contained the literal `#in-progress` while describing it, and Jira posted a comment truncated at exactly that point, then executed `#in-progress` as a transition.

This is not an edge case here. **This project references pull requests as `#nnn` constantly: 38 of the last 40 commit bodies contain such a token.** Left unaddressed, a routine commit body mentioning `#583` would silently truncate its Jira comment at that word and attempt a transition named `583`.

Two rules follow, and they are not optional:

1. **`#comment` goes LAST** in the message. Anything after it is consumed as its argument, so nothing else can be terminated by accident.
2. **The comment text contains no `#` at all.** Write pull-request references as `PR 583`, `pull/583`, or `GH-583`. Never `#583`.

The safe shape:

```
feat(reports): TBD-170 add utilization gauge

TBD-170 #comment Gauge over bar per design review. Follows PR 581; per-currency, never summed.
```

Note that this constraint applies only to commit bodies that carry a smart-commit command. Commits without one are unaffected, and `#nnn` in a PR *description* is never parsed.

**A consequence at merge time.** This repo squash-merges, and GitHub's squash subject is `<PR title> (#NNN)`. Since the PR title carries the issue key, the resulting subject reads `... (TBD-166) (#583)` and that `#583` would be parsed as a transition command named `583`.

In practice nothing happens at all, for two independent reasons: no such transition exists, and the squash commit's author email does not resolve to a Jira user anyway. Nothing to fix; worth not being alarmed by. See the squash-merge note above for the measured behaviour.

## 5. Automation rules

Transitions come from Jira automation, not from the agent. Automation does not forget, which is the actual fix for the drift problem.

**All rules are single-project, scoped to TBD.** This matters commercially: single-project rules do not draw on the monthly automation execution quota, only global and multi-project rules do. So rule volume here is free regardless of plan tier.

### Prerequisite: add an "In Review" status

The workflow currently has only To Do, In Progress and Done. The PR gate has nowhere to land. Before rule 2 can exist, add **In Review** (category "In Progress" / yellow) to the workflow used by project TBD.

*Project settings > Workflows > edit the active workflow > add status > publish.* Because existing transitions are global, the new status is reachable from anywhere without hand-wiring transitions.

### The rules

| # | Trigger | Condition | Action | Purpose |
|---|---|---|---|---|
| 1 | Branch created | branch name contains the issue key (implicit in the trigger) | Transition to **In Progress** | Work started, visible the moment a branch exists |
| 2 | Pull request created | none | Transition to **In Review** | The review gate becomes visible |
| 3 | Pull request merged | none | Transition to **Done** | Ship state, driven by the merge itself |
| 4 | Pull request declined | none | Transition to **To Do** | A closed-unmerged PR returns work to the queue rather than stranding it In Review |
| 5 | Build failed | none | Add comment: CI failed, with the build link. **No transition** | Surfaces red CI on the issue without yanking status out from under an active review |

Rule 5 deliberately does not transition. Red CI is an event during review, not a state change; transitioning on it would thrash status on every flaky run.

**Rule 3 and merge-to-main.** `main` requires one human approval and direct pushes are blocked, so rule 3 only ever fires on a genuine reviewed merge. It is safe as an unconditional transition to Done.

### Smart-commit enablement

Atlassian lists GitHub as a supported smart-commit source "after proper account linking and enabling Smart Commits." Whether the GitHub for Jira app exposes an explicit toggle (as the Bitbucket integration does) needs confirming in the app's configuration rather than assumed. Verify empirically: push a commit carrying `TBD-nnn #comment test` and check the issue. That is the only reliable check, because a disabled integration produces the same silence as a mismatched email.

### If "In Review" is not added

Rules 1, 3, 4 and 5 still work. Rule 2 is simply skipped, and a PR-open shows as a development-panel entry on an In Progress issue. The system degrades cleanly rather than breaking, so adding the status can wait without blocking anything else.

## 6. Division of labour

| Owner | Responsibility |
|---|---|
| **Automation rules** | PR-driven state that commits cannot express: PR opened, merged, declined, build failure |
| **Smart commits** | The narrative, agent-authored in the commit body: spec paths, decisions, review findings folded. Optionally `#in-progress` on a branch's first commit |
| **Agent via MCP** | Issue creation and refinement, labels, links, parking and unparking, and closing an item that shipped no code |
| **Operator in browser** | Project administration: statuses, automation rules, permissions, board configuration |

The dividing line: **automation owns what happened, smart commits and the agent own why.** An agent should never make a transition that a rule already covers, because duplicate transitions produce confusing double entries in the history.

Smart commits absorb most of what would otherwise be MCP comment calls, which is a real reduction in tool calls and keeps the explanation attached to the diff that caused it. MCP commenting remains for anything with no commit behind it, such as parking an item or recording a decision taken in conversation.

The one exception is closing an item with no code. That happens in this project (twice in July 2026, when a live `/impeccable` triage found two polish items already resolved) and no code event exists to trigger it, so the agent transitions to Done and comments the reason.

### The pickup gate

For the eventual autonomous agents, "is this ready to work?" resolves to:

```
project = TBD AND labels = ready AND labels != operator-only
  AND status = "To Do" AND labels not in (state-parked, state-gated, needs-decision)
```

An issue reaches `ready` only when a spec path sits in its description. The progression is `needs-design` to `needs-spec` to `has-spec` to `ready`. Nothing is agent-pickable on prose alone, which preserves the existing spec-first discipline rather than routing around it.

## 7. Dependency links

Links encode constraints that labels cannot:

- **Group C chain** is dependency-ordered. Its remaining items link in build order.
- **Group F cluster** all carry "is blocked by" against the Paddle payment-integration issue, because a single business decision unlocks the whole cluster. Modelling it as six independent parked items loses that fact.
- **PAT follow-ups** "relates to" the Group D authenticated-MCP item, since PATs are the auth substrate that item will build on.
- **CC utilization (F1)** "relates to" the Reports and Dashboard Epics, because it needs a surface in both.

## 8. Dual maintenance

**The roadmap stays primary.** It keeps the prose, the architect locks, and the decision history. Jira carries status, links and the PR trail. This split is deliberate: the roadmap is read into agent context every session and is cheap to grep, while Jira is the surface humans and automation see.

**Keys are inlined.** Each open roadmap bullet gains an inline `[TBD-nn]`, so the two sides never require fuzzy title matching to reconcile.

**Reconciliation is a phase in `end-session`,** not a separate ritual. Phase 1C:

1. For items touched this session, verify the Jira status matches reality. Automation should have handled it; a mismatch is a signal a rule misfired and needs reporting, not silent correction.
2. Mint issues for roadmap items created mid-session.
3. Backfill `[TBD-nn]` keys into the roadmap for anything newly minted.
4. Report drift rather than papering over it, so rule bugs surface instead of being masked by an agent cleaning up after them.

Step 4 matters. If the agent silently fixes what automation should have done, the automation stays broken forever.

New items created mid-session get their Jira issue immediately. It is a single call and defers nothing.

## 9. Out of scope

**Deployment tracking.** Deployments would appear in Jira only by adding `chrnorm/deployment-action` and a `.jira/config.yml` environment mapping to the GitHub Actions workflow. This project deploys through DO App Platform, so that means changing the deploy pipeline, with its own failure modes, for reporting value. Filed as its own Group J issue instead of bundled here.

**Components.** Labels cover the same ground. Components are worth defining only for per-area lead assignment, which does not apply to a single-operator project.

**Shipped history.** 230+ merged PRs stay in `git log main`. Recreating them as Done issues would duplicate git and bury the actionable backlog.

**Sprints and estimation.** A Sprint field exists but no board process needs it yet. Adding ceremony before it earns its keep is exactly the failure this project's pace guidance warns about.

## 10. Risks

**A second write target can drift.** Mitigated by making automation own the state that changes most often, and by making reconciliation report rather than silently repair.

**Automation rules are invisible to the agent.** No MCP tool can read or verify them. If a rule is disabled or misconfigured, the agent cannot tell, and will only notice as a status mismatch during reconcile. This is inherent to the capability boundary, and is the reason Phase 1C reports drift rather than fixing it.

**Issue keys in commit messages are a new habit.** A missed key means no build linkage for that PR. Not fatal, and self-correcting once noticed, but expect misses early.

**Smart commits fail silently by design.** A wrong committer email, a non-existent transition name, or a transition blocked by a required field all discard the command while the commit succeeds. Nothing in the git history indicates the command did not land. Treat a missing Jira comment as a signal to check the committer email first, since that is the failure mode with no error surface at all.

**Effort labels inherit the roadmap's optimism.** The roadmap's own Group H notes an item tagged S that turned out to need a hook redesign. Treat `effort-*` as the original estimate, not a verified one.
