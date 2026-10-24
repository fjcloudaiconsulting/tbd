# Security assessment tracker: October 2026

This is the shared record of the static security assessment of 2026-10-23. It
lists every finding, the pull request that addresses it and what is left to do.
Agents with Jira access use it to create, update and close the Jira issues.
Every agent working on this effort keeps it current, so the next one starts from
the real state and nobody needs a live conversation to catch up.

Last updated: 2026-10-24 by the assessment session, during the owner's merge run

## Disclosure note

This repository is public, and [SECURITY.md](../../SECURITY.md) asks that
security problems are not discussed in public. So this file lists every finding
by ID with a neutral title, its severity, status and fix, and **no exploit
detail**. Fixed findings are described in their PRs. The full assessment report
(`SECURITY_ASSESSMENT.md`, with affected code, impact and suggested fixes for
every finding) is held privately by the owner and is not committed. Do not paste
exploit detail into public issues, PRs or commit messages.

## Current state

- **Merged to `main`:** see the State column of the PR tables below.
- **In the owner's merge run:** the security PRs except #1007, the dependency
  PRs except #929, and this file (#1010, merged last). The run brings `main`
  into each PR, waits for every check, then squash-merges; a PR whose checks
  fail is reported and left open.
- **Waiting on the owner:** #1007 (check the production secrets first, item 1 of
  the owner decision queue) and #929 (PyYAML builds from source on Python 3.15).
- **Not released yet.** A merged fix reaches production only when the owner
  merges the release-please PR and then the image bump in `cloud-infra`.
- **Next:** finish the merge run; refresh the test durations on `main`; create
  and reconcile the Jira issues; work through the owner decision queue.

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
6. Restricted detail for an issue's description comes from the owner's private
   report, never from this file.

## Pull requests

| PR | Branch | Scope | Findings | State |
|---|---|---|---|---|
| [#1006](https://github.com/fjcloudaiconsulting/tbd/pull/1006) | `security/input-bounds` | Request input bounds | BE-01, BE-07, BE-08, BE-11, BE-13, BE-16, BE-22, BE-27 | Merged 2026-10-24 as `2f365949` |
| [#1009](https://github.com/fjcloudaiconsulting/tbd/pull/1009) | `security/auth-sessions` | Sign-in, MFA and sessions | BE-02, BE-04, BE-21, BE-25, BE-26, BE-32, DB-06, FE-02, FE-03 | Merged 2026-10-24 as `8b9c95a3` |
| [#1005](https://github.com/fjcloudaiconsulting/tbd/pull/1005) | `security/tenant-logic` | Billing, invitations, accounts, settings | BE-05, BE-06, BE-14, BE-15, BE-17, BE-19, BE-23 | Merged 2026-10-24 as `772ab8e3` |
| [#1008](https://github.com/fjcloudaiconsulting/tbd/pull/1008) | `security/egress-audit` | AI provider reads, CSP sink, admin, webhooks | BE-09, BE-10, BE-12, BE-20, BE-24, BE-29, BE-30, DB-02, FE-03 | Open |
| [#1004](https://github.com/fjcloudaiconsulting/tbd/pull/1004) | `security/frontend` | CSV export, token URLs, apex CSP, security.txt | FE-01, FE-03, FE-04, INF-14 | Open |
| [#1007](https://github.com/fjcloudaiconsulting/tbd/pull/1007) | `security/config-infra` | Production config, compose, export CLI, apex deploy, dependencies | INF-01, INF-03, INF-04, INF-05, INF-07, INF-10, INF-11, INF-15, DB-05 | Open: waiting on the production secret check |
| [#1010](https://github.com/fjcloudaiconsulting/tbd/pull/1010) | `docs/security-assessment-tracker` | This file | none | Open |

Dependency PRs handled in the same pass:

| PR | Change | State |
|---|---|---|
| [#1003](https://github.com/fjcloudaiconsulting/tbd/pull/1003) | Hold TypeScript below 7 in Renovate (replaces #937) | Open |
| [#978](https://github.com/fjcloudaiconsulting/tbd/pull/978) | uv 0.13.5 | Open |
| [#1002](https://github.com/fjcloudaiconsulting/tbd/pull/1002) | FastAPI 0.162.0 (routine Renovate update, not from the assessment) | Open |
| [#872](https://github.com/fjcloudaiconsulting/tbd/pull/872) | recharts 3.10.1; line charts animate unless the user prefers reduced motion | Open |
| [#941](https://github.com/fjcloudaiconsulting/tbd/pull/941) | react-resizable 4 | Open |
| [#929](https://github.com/fjcloudaiconsulting/tbd/pull/929) | Python 3.15 runtime | Open: waiting on the PyYAML build decision (owner queue item 12) |
| [#937](https://github.com/fjcloudaiconsulting/tbd/pull/937) | TypeScript 7 | Open: Renovate should close it once #1003 is on `main`; close it by hand otherwise |

## Merge notes

- Every backend PR regenerated `backend/.test_durations`, so later PRs
  conflicted on that file. The merge run combined both sides' entries; the
  durations fence passes on the fully merged tree. Once the run is done, run
  `gh workflow run test-durations.yml --ref main` and commit the artifact in a
  PR, as `backend/tests/test_test_durations_freshness.py` describes.
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
| BE-01 | Bound decimal inputs on amount fields | High | Fixed | #1006 | 2026-10-24 | `10dd5a3d` | | |
| BE-02 | Per-account limits on password and second-factor attempts | Medium | Fixed | #1009 | 2026-10-24 | `82c7c52e`, `591c50fe` | Follow-up: route the remaining password re-checks (password change, email change, token-minting step-ups) through the per-user window | |
| BE-03 | Email-code MFA fallback policy | Medium | Needs owner | | – | | Decide the email fallback policy for accounts with TOTP (options in the private report) | |
| BE-04 | End sessions on password reset and change | Medium | Fixed | #1009 | 2026-10-24 | `7ceb5679` | | |
| BE-05 | Self-service plan change restrictions | Medium | Partial | #1005 | 2026-10-24 | `f862b6c2` | Decide whether paid plans require payment once billing is live, and what canceled or past-due orgs are entitled to | |
| BE-06 | Invitation limits and response uniformity | Medium | Fixed | #1005 | 2026-10-24 | `933b79a1` | | |
| BE-07 | CSV import preview row cap | Medium | Fixed | #1006 | 2026-10-24 | `a90b05b6` | Follow-up: batch the per-row preview detectors | |
| BE-08 | Import confirm row cap | Medium | Fixed | #1006 | 2026-10-24 | `17e3630e` | Follow-up: chunked commits and an idempotent confirm | |
| BE-09 | Size cap on AI provider responses | Medium | Fixed | #1008 | no | `c55f200a` | | |
| BE-10 | Audit-log growth controls | Medium | Partial | #1008 | no | `9db358e9` | Set a retention period for `audit_events`; alert on `tbd.security.csp_reports{outcome="global_budget"}`; decide on deduplicating routine session-rotation audit rows and a per-session refresh limit | |
| BE-11 | Reports filter list bounds | Medium | Fixed | #1006 | 2026-10-24 | `18a5a090` | | |
| INF-01 | Origin client-IP trust | Medium | Partial | #1007 | no | `adb3004e` | Run the edge and origin checks listed in #1007 (cloud-infra) | |
| FE-01 | CORS origin list for the apex and www hosts | Low | Partial | #1004 | no | `d4b157be` | Set the production `BACKEND_CORS_ORIGINS` to the app origin only (cloud-infra) | |
| FE-02 | Google SSO callback token binding | Low | Fixed | #1009 | 2026-10-24 | `ff722648` | Deploy the backend before or with the frontend | |
| FE-03 | One-time tokens in URLs | Low | Partial | #1004, #1008, #1009 | partly | `33543891`, `3f00189c`, `582f71f9` | Delete existing stored CSP report rows; follow-up: send the invitation preview token in a request body | |
| FE-04 | CSV export formula neutralisation | Low | Fixed | #1004 | no | `3385f921` | | |
| BE-12 | Secret fields in validation error bodies | Low | Fixed | #1008 | no | `1254d326` | | |
| BE-13 | Upload size enforcement while reading | Low | Fixed | #1006 | 2026-10-24 | `cf41e6e8` | Set a request body size limit at the edge (cloud-infra) | |
| BE-14 | Role check on balance-moving account edits | Low | Fixed | #1005 | 2026-10-24 | `601acc54` | | |
| BE-15 | Concurrent balance update safety | Low | Fixed | #1005 | 2026-10-24 | `a8b9b95a` | | |
| BE-16 | Loan input validation | Low | Fixed | #1006 | 2026-10-24 | `e70cb11b` | | |
| BE-17 | Balance tracking when converting skipped import rows | Low | Fixed | #1005 | 2026-10-24 | `e5e3a023` | | |
| BE-18 | Shared tag dictionary contribution threshold | Low | Needs owner | | – | | Decide the contributor-counting policy (options in the private report) | |
| BE-19 | Reserved settings key normalisation | Low | Fixed | #1005 | 2026-10-24 | `1e55827e` | Check production `org_settings` for non-ASCII keys | |
| BE-20 | Interactive session for admin subscription and announcement writes | Low | Fixed | #1008 | no | `519234eb` | | |
| BE-21 | Atomic recovery-code consumption | Low | Fixed | #1009 | 2026-10-24 | `cf2ea62c` | | |
| BE-22 | Scenario simulation input bounds | Low | Partial | #1006 | 2026-10-24 | `962d4dd3` | Follow-up: run the simulation off the event loop | |
| BE-23 | Plan limit enforcement | Low | Partial | #1005 | 2026-10-24 | `b14936d2` | Decide seat limits during the beta and when to enforce the `plans` entitlement; follow-up: take the org and invitation locks in the same order on create and accept | |
| BE-24 | Mailgun webhook input validation | Low | Fixed | #1008 | no | `f504ae4e` | | |
| BE-25 | Login timing uniformity | Low | Fixed | #1009 | 2026-10-24 | `b813f523` | | |
| BE-32 | Second factor for MFA management and email change | Low | Fixed | #1009 | 2026-10-24 | `4091ef92`, `44740681` | | |
| DB-01 | Email collation for identity matching | Low | Needs owner | | – | | Decide on a collation migration for `users.email` | |
| DB-02 | Credentials in AI provider base URLs | Low | Partial | #1008 | no | `29cefd9e` | Find stored base URLs with credentials and ask those orgs to rotate | |
| DB-03 | Org-name uniqueness scope | Low | Needs owner | | – | | Decide whether org names stay globally unique | |
| DB-04 | Database principal separation | Low | Needs owner | | – | | Split migrator and runtime database users (cloud-infra) | |
| DB-05 | Export CLI file permissions | Low | Fixed | #1007 | no | `d803ce12` | | |
| INF-02 | MCP failed-auth ceiling keying | Low | Needs owner | | – | | Set `CLIENT_IP_HEADER` on the MCP workload (cloud-infra) | |
| INF-03 | Production secret validation coverage | Low | Fixed | #1007 | no | `f5d32fec` | Deploy gate: check production secrets before releasing #1007 | |
| INF-04 | `APP_ENV` default | Low | Partial | #1007 | no | `6c668014` | Confirm every production workload sets `APP_ENV`, then make the default fail-safe | |
| INF-05 | Captcha and first-user bootstrap defaults | Low | Partial | #1007 | no | `588344e3` | Decide on fail-closed captcha and a bootstrap token | |
| INF-06 | Reusable workflow pinning and secret inheritance | Low | Needs owner | | – | | Pin org workflows to SHAs; replace `secrets: inherit` | |
| INF-07 | Deploy secret isolation and wrangler pinning | Low | Partial | #1007 | no | `700aa0f3` | Split the apex build and deploy jobs | |
| INF-08 | Smoke-account identifier in history | Low | Needs owner | | – | | Rotate the smoke account identifier | |
| INF-09 | Health probe exposure | Low | Needs owner | | – | | Keep readiness probes off the public route (cloud-infra) | |
| INF-10 | Dev compose network exposure | Low | Fixed | #1007 | no | `0751d31d` | | |
| BE-26 | TOTP step reuse | Info | Partial | #1009 | 2026-10-24 | `82c7c52e` | Follow-up: apply the TOTP replay guard to the MFA checks on token minting | |
| BE-27 | LIKE escaping and free-text bounds | Info | Partial | #1006 | 2026-10-24 | `331053c0` | Follow-up: escape the tag-suggestion prefix | |
| BE-28 | Merchant dictionary vote attribution | Info | Not fixed | | – | | Follow-up: recompute votes server-side, one per org | |
| BE-29 | User merge foreign-key coverage | Info | Fixed | #1008 | no | `ad1a989f` | | |
| BE-30 | Typed superadmin-guard errors | Info | Fixed | #1008 | no | `9885ea52` | | |
| BE-31 | `agent:auto` scope behaviour | Info | Accepted | | – | | By design: reversible writes without confirmation | |
| DB-06 | Step-up token storage and key fingerprint | Info | Partial | #1009 | 2026-10-24 | `95d3bc67` | Follow-up: key the AI-key fingerprint with an HMAC | |
| INF-11 | `seed.py` in production images | Info | Fixed | #1007 | no | `412d4c33` | | |
| INF-12 | Legacy encrypted secrets in history | Info | Needs owner | | – | | Confirm the secrets in the legacy app spec were rotated | |
| INF-13 | Commit metadata emails | Info | Partial | | – | | Identity chosen and the assessment branches re-authored; older history keeps earlier addresses; delete the stale `chore/graphify-refresh-2026-10-15` branch | |
| INF-14 | `security.txt` | Info | Fixed | #1004 | no | `0d284c22` | Enable GitHub private vulnerability reporting; renew `Expires` yearly | |
| INF-15 | Dependency advisory audit | Info | Partial | #1007 | no | `f4b27914` | Decide on a `vite` override for dev-server advisories | |
| INF-16 | Settings outside this repo | Info | Needs owner | | – | | Add a minimum release age for uv in the shared Renovate preset | |

## Owner decision queue

Ordered by urgency. Each item is also an **Owner action** above.

1. **Before releasing #1007:** check that no production secret equals a value
   in `_PUBLIC_SECRET_VALUES` or starts with `dev-only`. The backend Deployment
   uses `strategy: Recreate`, so a match stops the API until that secret is
   rotated.
2. INF-01 and INF-02: the edge and origin checks in #1007.
3. BE-03: the email-code MFA fallback policy.
4. BE-05 and BE-23: billing-related enforcement.
5. BE-10: audit retention and the CSP drop alert.
6. FE-01: the production CORS list.
7. Production data clean-ups: stored CSP rows (FE-03), AI base URLs (DB-02),
   settings keys (BE-19).
8. BE-13, INF-04, INF-05, INF-09: production configuration.
9. INF-06, INF-07, INF-16: CI and Renovate settings.
10. BE-18, DB-01, DB-03, DB-04: data-model and product decisions.
11. INF-08, INF-12, INF-13, INF-14: repository and disclosure housekeeping.
12. Dependencies: PyYAML has no Python 3.15 wheels yet, so it builds from source
    in the image (#929); the TypeScript 7 hold (#1003).

## Handoff log

Newest first. Add your entry at the top; keep entries short and public-safe.

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
