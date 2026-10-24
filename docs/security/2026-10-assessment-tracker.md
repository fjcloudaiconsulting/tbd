# Security assessment tracker: October 2026

This is the shared record of the static security assessment of 2026-10-23. It
lists every finding, the pull request that addresses it and what is left to do.
Agents with Jira access use it to create, update and close the Jira issues.
Every agent working on this effort keeps it current, so the next one starts from
the real state and nobody needs a live conversation to catch up.

Last updated: 2026-10-24 by the Jira agent, after creating and reconciling the Jira issues

## Disclosure note

This repository is public, and [SECURITY.md](../../SECURITY.md) asks that
security problems are not discussed in public. So this file lists every finding
by ID with a neutral title, its severity, status and fix, and **no exploit
detail**. Fixed findings are described in their PRs. The full assessment report
(`SECURITY_ASSESSMENT.md`, with affected code, impact and suggested fixes for
every finding) is held privately by the owner and is not committed. Do not paste
exploit detail into public issues, PRs or commit messages.

## Current state

- **Merged to `main` on 2026-10-24:** #1006, #1009, #1005, #1008 and #1004
  (security), #1003, #978, #1002, #872 and #941 (dependencies), and #1010 (this
  file). Every check passed on each one before it merged. That covers 35 of the
  58 findings; see the Merged column.
- **Waiting on the owner:** #1007 (check the production secrets first, item 1 of
  the owner decision queue; its 9 findings are not merged) and #929 (PyYAML
  builds from source on Python 3.15). #937 was closed in favour of #1003.
- **Release:** the app is not released yet. The merged fixes ship in the
  release that the open release-please PR
  [#1011](https://github.com/fjcloudaiconsulting/tbd/pull/1011) prepares (0.303.1
  when this was written), once the owner merges it and then the image bump in `cloud-infra`. The apex landing site is
  the exception: it deploys from `main` on its own, so #1004's apex changes
  (connect-src and `security.txt`) have been live there since 2026-10-24.
- **Follow-ups merged:** #1014 (tracker refresh) and #1015 (test durations).
- **Jira:** every finding has an issue (Jira column), labelled
  `security-assessment-2026-10`. Merged Fixed and Partial findings are Done,
  with their remaining work in a linked follow-up or owner-decision issue. The
  #1007 findings are In Progress. Needs owner and Not fixed findings are open.
  The fix version is not set yet because 0.303.1 has not shipped.
- **Next:** the owner works through the decision queue (item 1 first) and
  releases #1011; the coding session takes the follow-ups and "Incoming from
  Jira" below.

## How to pick this up (any agent)

1. Read this whole file, then the handoff log at the end. The tables are a
   snapshot: confirm a PR's state with
   `gh pr view <n> --json state,mergedAt,mergeCommit` before acting on it.
2. Do your part (Jira, code, infrastructure checks, owner follow-ups).
3. Update this file in the same pass. You may change, add or remove anything:
   - change a row whose status, PR, Jira key or owner action moved;
   - add a row for a new finding or follow-up (next free ID in its layer, for
     example `BE-33`), or a row in the owner queue;
   - remove a row or section that no longer applies, and say why in the log;
   - keep the counts table in step with the findings table;
   - refresh the Merged and State columns with `gh` (the assessment session
     used a small script for this; a manual edit is fine).
4. Add an entry at the **top** of the handoff log: date, who you are, what you
   did, what you changed here, what comes next, and any questions for the
   owner. Do not rewrite other agents' entries; correct them in your own.
5. Ship the edit as a `docs(security): ...` PR from a fresh branch off `main`.
   First check that no other open PR edits this file; if one does, add to it or
   wait for it. The owner merges.
6. Keep it public-safe: neutral titles, no exploit detail, no secrets, no
   personal data.

## For the Jira agent

1. Use one Jira issue per finding ID. Put the ID first in the summary
   (`[BE-01] Bound decimal inputs on amount fields`) and add the label
   `security-assessment-2026-10`. Search for an existing issue with that ID or
   label before creating one.
2. Priority from severity: High → High, Medium → Medium, Low → Low,
   Info → Lowest.
3. Record the outcome on each issue: the status here, the PR link(s), the
   squash commit on `main` (State column of the PR tables), and the **Owner
   action** text as the remaining work. The **Commit** column names the
   finding's commit on its PR branch; after the squash merge it is still visible
   in the PR's Commits tab, but not on `main`.
4. Transition rules:
   - **Fixed**, and the Merged column shows a date: Done. Comment with the PR
     and its squash commit, and set the fix version once release-please
     publishes the release that contains it.
   - **Fixed**, not merged yet: In review, linked to the PR.
   - **Partial**: when the PR is merged, comment what was done. Then either keep
     the issue open for the remaining work, or close it and open a follow-up
     issue for the rest. Record both keys in the Jira column
     (`TBD-1, follow-up TBD-2`).
   - **Needs owner** / **Not fixed**: open, assigned to the owner, with the
     decision or follow-up from **Owner action**.
   - **Accepted**: close as Won't Do, with the reason.
5. Write each Jira key into the **Jira** column, following "How to pick this
   up".
6. The TBD workflow has only To Do, In Progress and Done. "In review" maps to
   In Progress, and "Won't Do" to Done with the reason in a comment.
7. Restricted detail for an issue's description comes from the owner's private
   report, never from this file.

## Pull requests

| PR | Branch | Scope | Findings | State |
|---|---|---|---|---|
| [#1006](https://github.com/fjcloudaiconsulting/tbd/pull/1006) | `security/input-bounds` | Request input bounds | BE-01, BE-07, BE-08, BE-11, BE-13, BE-16, BE-22, BE-27 | Merged 2026-10-24 as `2f365949` |
| [#1009](https://github.com/fjcloudaiconsulting/tbd/pull/1009) | `security/auth-sessions` | Sign-in, MFA and sessions | BE-02, BE-04, BE-21, BE-25, BE-26, BE-32, DB-06, FE-02, FE-03 | Merged 2026-10-24 as `8b9c95a3` |
| [#1005](https://github.com/fjcloudaiconsulting/tbd/pull/1005) | `security/tenant-logic` | Billing, invitations, accounts, settings | BE-05, BE-06, BE-14, BE-15, BE-17, BE-19, BE-23 | Merged 2026-10-24 as `772ab8e3` |
| [#1008](https://github.com/fjcloudaiconsulting/tbd/pull/1008) | `security/egress-audit` | AI provider reads, CSP sink, admin, webhooks | BE-09, BE-10, BE-12, BE-20, BE-24, BE-29, BE-30, DB-02, FE-03 | Merged 2026-10-24 as `7f478681` |
| [#1004](https://github.com/fjcloudaiconsulting/tbd/pull/1004) | `security/frontend` | CSV export, token URLs, apex CSP, security.txt | FE-01, FE-03, FE-04, INF-14 | Merged 2026-10-24 as `47b8e9f0` |
| [#1007](https://github.com/fjcloudaiconsulting/tbd/pull/1007) | `security/config-infra` | Production config, compose, export CLI, apex deploy, dependencies | INF-01, INF-03, INF-04, INF-05, INF-07, INF-10, INF-11, INF-15, DB-05 | Open: waiting on the production secret check |
| [#1010](https://github.com/fjcloudaiconsulting/tbd/pull/1010) | `docs/security-assessment-tracker` | This file | none | Merged 2026-10-24 as `35caaab9` |

Dependency PRs handled in the same pass:

| PR | Change | State |
|---|---|---|
| [#1003](https://github.com/fjcloudaiconsulting/tbd/pull/1003) | Hold TypeScript below 7 in Renovate (replaces #937) | Merged 2026-10-24 as `993ebec1` |
| [#978](https://github.com/fjcloudaiconsulting/tbd/pull/978) | uv 0.13.5 | Merged 2026-10-24 as `c9203f83` |
| [#1002](https://github.com/fjcloudaiconsulting/tbd/pull/1002) | FastAPI 0.162.0 (routine Renovate update, not from the assessment) | Merged 2026-10-24 as `0e6fe9a7` |
| [#872](https://github.com/fjcloudaiconsulting/tbd/pull/872) | recharts 3.10.1; line charts animate unless the user prefers reduced motion | Merged 2026-10-24 as `238c793a` |
| [#941](https://github.com/fjcloudaiconsulting/tbd/pull/941) | react-resizable 4 | Merged 2026-10-24 as `effc9098` |
| [#929](https://github.com/fjcloudaiconsulting/tbd/pull/929) | Python 3.15 runtime | Open: waiting on the PyYAML build decision (owner queue item 12) |
| [#937](https://github.com/fjcloudaiconsulting/tbd/pull/937) | TypeScript 7 (replaced by #1003) | Closed |

## Merge notes

- Every backend PR regenerated `backend/.test_durations`, so later PRs
  conflicted on that file. The merge run combined both sides' entries, and the
  durations fence passes on `main`. A fresh harvest from the test-durations
  workflow on `main` is in
  [#1015](https://github.com/fjcloudaiconsulting/tbd/pull/1015).
- When #1007 is merged after #1004, `docs/operations/ENVIRONMENT.md` conflicts
  on the `BACKEND_CORS_ORIGINS` row: keep #1004's row and append #1007's
  sentence about the `config.production_open_default` warning.

## Findings

Status values: Fixed, Partial, Needs owner, Not fixed, Accepted. **Merged**
shows the date every listed PR reached `main`, `partly` when only some have,
`no` when none has, and `–` when there is no PR.

| Severity | Total | Fixed | Partial | Needs owner | Not fixed | Accepted |
|---|---|---|---|---|---|---|
| High | 1 | 1 | 0 | 0 | 0 | 0 |
| Medium | 11 | 7 | 3 | 1 | 0 | 0 |
| Low | 33 | 17 | 8 | 8 | 0 | 0 |
| Info | 13 | 4 | 5 | 2 | 1 | 1 |
| **All** | **58** | **29** | **16** | **11** | **1** | **1** |

| ID | Title | Severity | Status | PR | Merged | Commit | Owner action | Jira |
|---|---|---|---|---|---|---|---|---|
| BE-01 | Bound decimal inputs on amount fields | High | Fixed | #1006 | 2026-10-24 | `10dd5a3d` | | TBD-672 |
| BE-02 | Per-account limits on password and second-factor attempts | Medium | Fixed | #1009 | 2026-10-24 | `82c7c52e`, `591c50fe` | Follow-up: route the remaining password re-checks (password change, email change, token-minting step-ups) through the per-user window | TBD-677, follow-up TBD-735 |
| BE-03 | Email-code MFA fallback policy | Medium | Needs owner | | – | | Decide the email fallback policy for accounts with TOTP (options in the private report) | TBD-680 |
| BE-04 | End sessions on password reset and change | Medium | Fixed | #1009 | 2026-10-24 | `7ceb5679` | | TBD-685 |
| BE-05 | Self-service plan change restrictions | Medium | Partial | #1005 | 2026-10-24 | `f862b6c2` | Decide whether paid plans require payment once billing is live, and what canceled or past-due orgs are entitled to | TBD-689 |
| BE-06 | Invitation limits and response uniformity | Medium | Fixed | #1005 | 2026-10-24 | `933b79a1` | | TBD-693 |
| BE-07 | CSV import preview row cap | Medium | Fixed | #1006 | 2026-10-24 | `a90b05b6` | Follow-up: batch the per-row preview detectors | TBD-697, follow-up TBD-736 |
| BE-08 | Import confirm row cap | Medium | Fixed | #1006 | 2026-10-24 | `17e3630e` | Follow-up: chunked commits and an idempotent confirm | TBD-701, follow-up TBD-737 |
| BE-09 | Size cap on AI provider responses | Medium | Fixed | #1008 | 2026-10-24 | `c55f200a` | | TBD-705 |
| BE-10 | Audit-log growth controls | Medium | Partial | #1008 | 2026-10-24 | `9db358e9` | Set a retention period for `audit_events`; alert on `tbd.security.csp_reports{outcome="global_budget"}`; decide on deduplicating routine session-rotation audit rows and a per-session refresh limit | TBD-709 |
| BE-11 | Reports filter list bounds | Medium | Fixed | #1006 | 2026-10-24 | `18a5a090` | | TBD-713 |
| INF-01 | Origin client-IP trust | Medium | Partial | #1007 | no | `adb3004e` | Run the edge and origin checks listed in #1007 (cloud-infra) | TBD-717 |
| FE-01 | CORS origin list for the apex and www hosts | Low | Partial | #1004 | 2026-10-24 | `d4b157be` | Set the production `BACKEND_CORS_ORIGINS` to the app origin only (cloud-infra) | TBD-720 |
| FE-02 | Google SSO callback token binding | Low | Fixed | #1009 | 2026-10-24 | `ff722648` | Deploy the backend before or with the frontend | TBD-728 |
| FE-03 | One-time tokens in URLs | Low | Partial | #1004, #1008, #1009 | 2026-10-24 | `33543891`, `3f00189c`, `582f71f9` | Delete existing stored CSP report rows; follow-up: send the invitation preview token in a request body | TBD-729, follow-up TBD-738 |
| FE-04 | CSV export formula neutralisation | Low | Fixed | #1004 | 2026-10-24 | `3385f921` | | TBD-730 |
| BE-12 | Secret fields in validation error bodies | Low | Fixed | #1008 | 2026-10-24 | `1254d326` | | TBD-731 |
| BE-13 | Upload size enforcement while reading | Low | Fixed | #1006 | 2026-10-24 | `cf41e6e8` | Set a request body size limit at the edge (cloud-infra) | TBD-732 |
| BE-14 | Role check on balance-moving account edits | Low | Fixed | #1005 | 2026-10-24 | `601acc54` | | TBD-733 |
| BE-15 | Concurrent balance update safety | Low | Fixed | #1005 | 2026-10-24 | `a8b9b95a` | | TBD-734 |
| BE-16 | Loan input validation | Low | Fixed | #1006 | 2026-10-24 | `e70cb11b` | | TBD-674 |
| BE-17 | Balance tracking when converting skipped import rows | Low | Fixed | #1005 | 2026-10-24 | `e5e3a023` | | TBD-676 |
| BE-18 | Shared tag dictionary contribution threshold | Low | Needs owner | | – | | Decide the contributor-counting policy (options in the private report) | TBD-678 |
| BE-19 | Reserved settings key normalisation | Low | Fixed | #1005 | 2026-10-24 | `1e55827e` | Check production `org_settings` for non-ASCII keys | TBD-682 |
| BE-20 | Interactive session for admin subscription and announcement writes | Low | Fixed | #1008 | 2026-10-24 | `519234eb` | | TBD-684 |
| BE-21 | Atomic recovery-code consumption | Low | Fixed | #1009 | 2026-10-24 | `cf2ea62c` | | TBD-687 |
| BE-22 | Scenario simulation input bounds | Low | Partial | #1006 | 2026-10-24 | `962d4dd3` | Follow-up: run the simulation off the event loop | TBD-690, follow-up TBD-726 |
| BE-23 | Plan limit enforcement | Low | Partial | #1005 | 2026-10-24 | `b14936d2` | Decide seat limits during the beta and when to enforce the `plans` entitlement; follow-up: take the org and invitation locks in the same order on create and accept | TBD-696, follow-up TBD-727 |
| BE-24 | Mailgun webhook input validation | Low | Fixed | #1008 | 2026-10-24 | `f504ae4e` | | TBD-699 |
| BE-25 | Login timing uniformity | Low | Fixed | #1009 | 2026-10-24 | `b813f523` | | TBD-702 |
| BE-32 | Second factor for MFA management and email change | Low | Fixed | #1009 | 2026-10-24 | `4091ef92`, `44740681` | | TBD-706 |
| DB-01 | Email collation for identity matching | Low | Needs owner | | – | | Decide on a collation migration for `users.email` | TBD-708 |
| DB-02 | Credentials in AI provider base URLs | Low | Partial | #1008 | 2026-10-24 | `29cefd9e` | Find stored base URLs with credentials and ask those orgs to rotate | TBD-711 |
| DB-03 | Org-name uniqueness scope | Low | Needs owner | | – | | Decide whether org names stay globally unique | TBD-715 |
| DB-04 | Database principal separation | Low | Needs owner | | – | | Split migrator and runtime database users (cloud-infra) | TBD-721 |
| DB-05 | Export CLI file permissions | Low | Fixed | #1007 | no | `d803ce12` | | TBD-722 |
| INF-02 | MCP failed-auth ceiling keying | Low | Needs owner | | – | | Set `CLIENT_IP_HEADER` on the MCP workload (cloud-infra) | TBD-723 |
| INF-03 | Production secret validation coverage | Low | Fixed | #1007 | no | `f5d32fec` | Deploy gate: check production secrets before releasing #1007 | TBD-724 |
| INF-04 | `APP_ENV` default | Low | Partial | #1007 | no | `6c668014` | Confirm every production workload sets `APP_ENV`, then make the default fail-safe | TBD-725 |
| INF-05 | Captcha and first-user bootstrap defaults | Low | Partial | #1007 | no | `588344e3` | Decide on fail-closed captcha and a bootstrap token | TBD-673 |
| INF-06 | Reusable workflow pinning and secret inheritance | Low | Needs owner | | – | | Pin org workflows to SHAs; replace `secrets: inherit` | TBD-675 |
| INF-07 | Deploy secret isolation and wrangler pinning | Low | Partial | #1007 | no | `700aa0f3` | Split the apex build and deploy jobs | TBD-679 |
| INF-08 | Smoke-account identifier in history | Low | Needs owner | | – | | Rotate the smoke account identifier | TBD-681 |
| INF-09 | Health probe exposure | Low | Needs owner | | – | | Keep readiness probes off the public route (cloud-infra) | TBD-683 |
| INF-10 | Dev compose network exposure | Low | Fixed | #1007 | no | `0751d31d` | | TBD-686 |
| BE-26 | TOTP step reuse | Info | Partial | #1009 | 2026-10-24 | `82c7c52e` | Follow-up: apply the TOTP replay guard to the MFA checks on token minting | TBD-688, follow-up TBD-716 |
| BE-27 | LIKE escaping and free-text bounds | Info | Partial | #1006 | 2026-10-24 | `331053c0` | Follow-up: escape the tag-suggestion prefix | TBD-691, follow-up TBD-718 |
| BE-28 | Merchant dictionary vote attribution | Info | Not fixed | | – | | Follow-up: recompute votes server-side, one per org | TBD-692 |
| BE-29 | User merge foreign-key coverage | Info | Fixed | #1008 | 2026-10-24 | `ad1a989f` | | TBD-694 |
| BE-30 | Typed superadmin-guard errors | Info | Fixed | #1008 | 2026-10-24 | `9885ea52` | | TBD-695 |
| BE-31 | `agent:auto` scope behaviour | Info | Accepted | | – | | By design: reversible writes without confirmation | TBD-698 |
| DB-06 | Step-up token storage and key fingerprint | Info | Partial | #1009 | 2026-10-24 | `95d3bc67` | Follow-up: key the AI-key fingerprint with an HMAC | TBD-700, follow-up TBD-719 |
| INF-11 | `seed.py` in production images | Info | Fixed | #1007 | no | `412d4c33` | | TBD-703 |
| INF-12 | Legacy encrypted secrets in history | Info | Needs owner | | – | | Confirm the secrets in the legacy app spec were rotated | TBD-704 |
| INF-13 | Commit metadata emails | Info | Partial | | – | | Identity chosen and the assessment branches re-authored; older history keeps earlier addresses; delete the stale `chore/graphify-refresh-2026-10-15` branch | TBD-707 |
| INF-14 | `security.txt` | Info | Fixed | #1004 | 2026-10-24 | `0d284c22` | Enable GitHub private vulnerability reporting; renew `Expires` yearly | TBD-710 |
| INF-15 | Dependency advisory audit | Info | Partial | #1007 | no | `f4b27914` | Decide on a `vite` override for dev-server advisories | TBD-712 |
| INF-16 | Settings outside this repo | Info | Needs owner | | – | | Add a minimum release age for uv in the shared Renovate preset | TBD-714 |

## Owner decision queue

Ordered by urgency. Each item is also an **Owner action** above.

1. (TBD-739) **Before releasing #1007:** check that no production secret equals a value
   in `_PUBLIC_SECRET_VALUES` or starts with `dev-only`. The backend Deployment
   uses `strategy: Recreate`, so a match stops the API until that secret is
   rotated.
2. (TBD-740) INF-01 and INF-02: the edge and origin checks in #1007.
3. (TBD-680) BE-03: the email-code MFA fallback policy.
4. (TBD-741) BE-05 and BE-23: billing-related enforcement.
5. (TBD-742) BE-10: audit retention and the CSP drop alert.
6. (TBD-743) FE-01: the production CORS list.
7. (TBD-744) Production data clean-ups: stored CSP rows (FE-03), AI base URLs (DB-02),
   settings keys (BE-19).
8. (TBD-745) BE-13, INF-04, INF-05, INF-09: production configuration.
9. (TBD-746) INF-06, INF-07, INF-16: CI and Renovate settings.
10. BE-18, DB-01, DB-03, DB-04 (each finding's own issue): data-model and product decisions.
11. (TBD-747) INF-08, INF-12, INF-13, INF-14: repository and disclosure housekeeping.
12. (TBD-748) Dependencies: PyYAML has no Python 3.15 wheels yet, so it builds from source
    in the image (#929); the TypeScript 7 hold (#1003).

## Incoming from Jira

Open TBD issues outside this assessment that touch the same work. Neutral
titles only; the detail is in Jira.

| Jira | Topic | Related finding | Suggested action |
|---|---|---|---|
| TBD-653 | Sign-in and MFA attempt limits (earlier audit) | BE-02, BE-26 | Most of it shipped in #1009; check the rest against the follow-ups, then close |
| TBD-611 | Login timing (earlier audit) | BE-25 | Shipped in #1009; confirm and close |
| TBD-654 | Auth hardening lows (earlier audit) | BE-25, FE-03 | Partly shipped in #1009; check each item, then narrow or close |
| TBD-609 | AI credential base URL handling | DB-02 | Shipped in #1008; confirm and close |
| TBD-655 | Database user separation | DB-04 | Same work as DB-04; keep one |
| TBD-477, TBD-401, TBD-475 | CSP report writer, audit retention, audit UI | BE-10 | The writer is bounded by #1008; retention is owner item 5 |
| TBD-607 | AI adapter error handling | BE-09 | Related hardening in the same code; still open |
| TBD-661 | Founder-count endpoint and apex connect-src | FE-01 | Follows the CORS narrowing |
| TBD-656 | Supply-chain and CI settings | INF-06, INF-07 | Same area as owner item 9 |
| TBD-442 | First-user setup | INF-05 | Same area as owner item 8 |
| TBD-610 | MCP server deployment | INF-02 | Set the client-IP header when it deploys |
| TBD-658, TBD-659, TBD-639 | Python, Renovate and toolchain upgrades | INF-15 | Same decision as owner item 12 |
| TBD-749 | A KPI test fails between local and UTC midnight | none | Raised during the assessment; small test fix |

## Handoff log

Newest first. Add your entry at the top; keep entries short and public-safe.

### 2026-10-24, Jira reconciliation: Jira agent (Claude Code, with Jira access)

- **Did:** created one Jira issue per finding (58), nine follow-ups linked to
  their findings, ten owner-decision tasks (queue items 1, 2, 4 to 9, 11 and
  12; items 3 and 10 are the findings' own issues) and one task for the KPI
  test. Recorded the outcome, PR links and a comment on each, and closed the
  merged findings. Linked the earlier overlapping issues listed in "Incoming
  from Jira".
- **Changed here:** the Jira column, Jira keys in the owner queue, Current
  state, the workflow note for the Jira agent, and the new "Incoming from Jira"
  section. Item 13 of the private queue (merge order) is done and dropped.
- **Next:** the owner works through the queue, item 1 first, and releases
  #1011, then sets the fix version on the closed issues. The coding session
  takes the follow-ups and the "Incoming from Jira" checks.
- **Questions for the owner:** the decision queue; and whether to close the
  earlier overlapping issues once the coding session confirms them.

### 2026-10-24, after the merge run: assessment session

- **Did:** the owner's merge run merged eleven PRs, each with every check
  passing (see Current state). Harvested the test durations on `main` (#1015).
- **Changed here:** refreshed the State and Merged columns, Current state,
  release notes and merge notes.
- **Next:** the Jira agent can start now. Close the Fixed findings whose Merged
  column has a date, with the release from #1011 as the fix version once it ships. Keep the
  #1007 findings in review, and handle Partial and Needs owner as described
  above. The owner merges this PR and #1015.
- **Questions for the owner:** the release (#1011) and the queue items, item 1
  first.

### 2026-10-24: assessment session (Claude Code in the repo checkout, no Jira access)

- **Did:** ran the assessment (58 findings) and opened the six fix PRs, this
  tracker and the dependency PRs. Re-authored the PR commits to the owner's
  chosen identity and updated every commit ID in this file and in the PR
  descriptions. The owner then started the merge run.
- **Changed here:** restructured the file for handoffs: Current state, How to
  pick this up, the Merged and State columns, merge notes and this log. The
  Jira rule for Fixed is now Done on merge, with the release recorded as the
  fix version.
- **Next:** this session refreshes the file when the merge run ends and
  re-harvests the test durations on `main`. The Jira agent creates or finds an
  issue per finding, records the outcome, closes the merged Fixed ones, and
  writes the keys back here.
- **Questions for the owner:** none beyond the decision queue.
