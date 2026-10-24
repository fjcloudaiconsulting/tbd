# Security assessment tracker: October 2026

Status of every finding from the static security assessment of 2026-10-23 and
the pull requests that address them. It exists so an agent with Jira access can
record each finding, reconcile it with the TBD project, and keep both in step.

## Disclosure note

This repository is public, and [SECURITY.md](../../SECURITY.md) asks that
security problems are not discussed in public. So this file lists every finding
by ID with a neutral title, its severity, status and fix, and **no exploit
detail**. Fixed findings are described in their PRs. The full assessment report
(`SECURITY_ASSESSMENT.md`, with affected code, impact and suggested fixes for
every finding) is held privately by the owner and is not committed. Do not paste
exploit detail into public issues, PRs or commit messages.

## For the Jira reconciliation agent

1. Use one Jira issue per finding ID. Put the ID first in the summary
   (`[BE-01] Bound decimal inputs on amount fields`) and add the label
   `security-assessment-2026-10`. Search for an existing issue with that ID or
   label before creating one.
2. Priority from severity: High → High, Medium → Medium, Low → Low,
   Info → Lowest.
3. Link the PR(s) in the **PR** column. Commit SHAs are the finding's commit on
   its PR branch (PRs are squash-merged, so the SHA will not exist on `main`).
4. Transition rules:
   - **Fixed**: Done once the PR is merged and released.
   - **Partial**: keep open after the PR merges; the remaining work is in
     **Owner action**.
   - **Needs owner** / **Not fixed**: open, assigned to the owner, with the
     decision in **Owner action**.
   - **Accepted**: close as Won't Do, with the reason.
5. Write the Jira key into the **Jira** column through a `docs:` PR, so this file
   and Jira stay reconciled. When a status changes, update the row here in the
   same PR.
6. Restricted detail for an issue's description comes from the owner's private
   report, never from this file.

## Pull requests

| PR | Branch | Scope | Findings |
|---|---|---|---|
| [#1006](https://github.com/fjcloudaiconsulting/tbd/pull/1006) | `security/input-bounds` | Request input bounds | BE-01, BE-07, BE-08, BE-11, BE-13, BE-16, BE-22, BE-27 |
| [#1009](https://github.com/fjcloudaiconsulting/tbd/pull/1009) | `security/auth-sessions` | Sign-in, MFA and sessions | BE-02, BE-04, BE-21, BE-25, BE-26, BE-32, DB-06, FE-02, FE-03 |
| [#1005](https://github.com/fjcloudaiconsulting/tbd/pull/1005) | `security/tenant-logic` | Billing, invitations, accounts, settings | BE-05, BE-06, BE-14, BE-15, BE-17, BE-19, BE-23 |
| [#1008](https://github.com/fjcloudaiconsulting/tbd/pull/1008) | `security/egress-audit` | AI provider reads, CSP sink, admin, webhooks | BE-09, BE-10, BE-12, BE-20, BE-24, BE-29, BE-30, DB-02, FE-03 |
| [#1007](https://github.com/fjcloudaiconsulting/tbd/pull/1007) | `security/config-infra` | Production config, compose, export CLI, apex deploy, dependencies | INF-01, INF-03, INF-04, INF-05, INF-07, INF-10, INF-11, INF-15, DB-05 |
| [#1004](https://github.com/fjcloudaiconsulting/tbd/pull/1004) | `security/frontend` | CSV export, token URLs, apex CSP, security.txt | FE-01, FE-03, FE-04, INF-14 |

Suggested merge order: #1006 (the High), #1009, #1005, #1008, #1004, then
#1007 last because of its deploy gate (see the owner queue). Every backend PR
regenerates `backend/.test_durations`. When a later PR conflicts on that file,
take either side: each is a full harvest of its branch and stays well above the
coverage floor. Then run `gh workflow run test-durations.yml --ref main` once all
are merged and commit the artifact. `docs/operations/ENVIRONMENT.md` conflicts
between #1004 and #1007 on the `BACKEND_CORS_ORIGINS` row; keep #1004's row and
append #1007's sentence about the `config.production_open_default` warning.

Dependency PRs handled in the same pass: #978 (uv 0.13.5, CI re-run),
#929 (Python 3.15 runtime, four fixes), #872 (recharts 3.10.1, chart animation
tripwire), #941 (react-resizable 4, no change needed), and #1003 (hold
TypeScript below 7, which replaces #937).

## Findings

Status values: Fixed, Partial, Needs owner, Not fixed, Accepted.

| Severity | Total | Fixed | Partial | Needs owner | Not fixed | Accepted |
|---|---|---|---|---|---|---|
| High | 1 | 1 | 0 | 0 | 0 | 0 |
| Medium | 11 | 7 | 3 | 1 | 0 | 0 |
| Low | 33 | 17 | 8 | 8 | 0 | 0 |
| Info | 13 | 4 | 5 | 2 | 1 | 1 |
| **All** | **58** | **29** | **16** | **11** | **1** | **1** |

| ID | Title | Severity | Status | PR | Commit | Owner action | Jira |
|---|---|---|---|---|---|---|---|
| BE-01 | Bound decimal inputs on amount fields | High | Fixed | #1006 | `10dd5a3d` | | |
| BE-02 | Per-account limits on password and second-factor attempts | Medium | Fixed | #1009 | `82c7c52e`, `591c50fe` | Follow-up: route the remaining password re-checks (password change, email change, token-minting step-ups) through the per-user window | |
| BE-03 | Email-code MFA fallback policy | Medium | Needs owner | | | Decide the email fallback policy for accounts with TOTP (options in the private report) | |
| BE-04 | End sessions on password reset and change | Medium | Fixed | #1009 | `7ceb5679` |  | |
| BE-05 | Self-service plan change restrictions | Medium | Partial | #1005 | `f862b6c2` | Decide whether paid plans require payment once billing is live, and what canceled or past-due orgs are entitled to | |
| BE-06 | Invitation limits and response uniformity | Medium | Fixed | #1005 | `933b79a1` | | |
| BE-07 | CSV import preview row cap | Medium | Fixed | #1006 | `a90b05b6` | Follow-up: batch the per-row preview detectors | |
| BE-08 | Import confirm row cap | Medium | Fixed | #1006 | `17e3630e` | Follow-up: chunked commits and an idempotent confirm | |
| BE-09 | Size cap on AI provider responses | Medium | Fixed | #1008 | `c55f200a` | | |
| BE-10 | Audit-log growth controls | Medium | Partial | #1008 | `9db358e9` | Set a retention period for `audit_events`; alert on `tbd.security.csp_reports{outcome="global_budget"}`; decide on deduplicating routine session-rotation audit rows and a per-session refresh limit | |
| BE-11 | Reports filter list bounds | Medium | Fixed | #1006 | `18a5a090` | | |
| INF-01 | Origin client-IP trust | Medium | Partial | #1007 | `adb3004e` | Run the edge and origin checks listed in #1007 (cloud-infra) | |
| FE-01 | CORS origin list for the apex and www hosts | Low | Partial | #1004 | `d4b157be` | Set the production `BACKEND_CORS_ORIGINS` to the app origin only (cloud-infra) | |
| FE-02 | Google SSO callback token binding | Low | Fixed | #1009 | `ff722648` | Deploy the backend before or with the frontend | |
| FE-03 | One-time tokens in URLs | Low | Partial | #1004, #1008, #1009 | `33543891`, `3f00189c`, `582f71f9` | Delete existing stored CSP report rows; follow-up: send the invitation preview token in a request body | |
| FE-04 | CSV export formula neutralisation | Low | Fixed | #1004 | `3385f921` | | |
| BE-12 | Secret fields in validation error bodies | Low | Fixed | #1008 | `1254d326` | | |
| BE-13 | Upload size enforcement while reading | Low | Fixed | #1006 | `cf41e6e8` | Set a request body size limit at the edge (cloud-infra) | |
| BE-14 | Role check on balance-moving account edits | Low | Fixed | #1005 | `601acc54` | | |
| BE-15 | Concurrent balance update safety | Low | Fixed | #1005 | `a8b9b95a` | | |
| BE-16 | Loan input validation | Low | Fixed | #1006 | `e70cb11b` | | |
| BE-17 | Balance tracking when converting skipped import rows | Low | Fixed | #1005 | `e5e3a023` | | |
| BE-18 | Shared tag dictionary contribution threshold | Low | Needs owner | | | Decide the contributor-counting policy (options in the private report) | |
| BE-19 | Reserved settings key normalisation | Low | Fixed | #1005 | `1e55827e` | Check production `org_settings` for non-ASCII keys | |
| BE-20 | Interactive session for admin subscription and announcement writes | Low | Fixed | #1008 | `519234eb` | | |
| BE-21 | Atomic recovery-code consumption | Low | Fixed | #1009 | `cf2ea62c` |  | |
| BE-22 | Scenario simulation input bounds | Low | Partial | #1006 | `962d4dd3` | Follow-up: run the simulation off the event loop | |
| BE-23 | Plan limit enforcement | Low | Partial | #1005 | `b14936d2` | Decide seat limits during the beta and when to enforce the `plans` entitlement; follow-up: take the org and invitation locks in the same order on create and accept | |
| BE-24 | Mailgun webhook input validation | Low | Fixed | #1008 | `f504ae4e` | | |
| BE-25 | Login timing uniformity | Low | Fixed | #1009 | `b813f523` |  | |
| BE-32 | Second factor for MFA management and email change | Low | Fixed | #1009 | `4091ef92`, `44740681` |  | |
| DB-01 | Email collation for identity matching | Low | Needs owner | | | Decide on a collation migration for `users.email` | |
| DB-02 | Credentials in AI provider base URLs | Low | Partial | #1008 | `29cefd9e` | Find stored base URLs with credentials and ask those orgs to rotate | |
| DB-03 | Org-name uniqueness scope | Low | Needs owner | | | Decide whether org names stay globally unique | |
| DB-04 | Database principal separation | Low | Needs owner | | | Split migrator and runtime database users (cloud-infra) | |
| DB-05 | Export CLI file permissions | Low | Fixed | #1007 | `d803ce12` | | |
| INF-02 | MCP failed-auth ceiling keying | Low | Needs owner | | | Set `CLIENT_IP_HEADER` on the MCP workload (cloud-infra) | |
| INF-03 | Production secret validation coverage | Low | Fixed | #1007 | `f5d32fec` | Deploy gate: check production secrets before releasing #1007 | |
| INF-04 | `APP_ENV` default | Low | Partial | #1007 | `6c668014` | Confirm every production workload sets `APP_ENV`, then make the default fail-safe | |
| INF-05 | Captcha and first-user bootstrap defaults | Low | Partial | #1007 | `588344e3` | Decide on fail-closed captcha and a bootstrap token | |
| INF-06 | Reusable workflow pinning and secret inheritance | Low | Needs owner | | | Pin org workflows to SHAs; replace `secrets: inherit` | |
| INF-07 | Deploy secret isolation and wrangler pinning | Low | Partial | #1007 | `700aa0f3` | Split the apex build and deploy jobs | |
| INF-08 | Smoke-account identifier in history | Low | Needs owner | | | Rotate the smoke account identifier | |
| INF-09 | Health probe exposure | Low | Needs owner | | | Keep readiness probes off the public route (cloud-infra) | |
| INF-10 | Dev compose network exposure | Low | Fixed | #1007 | `0751d31d` | | |
| BE-26 | TOTP step reuse | Info | Partial | #1009 | `82c7c52e` | Follow-up: apply the TOTP replay guard to the MFA checks on token minting | |
| BE-27 | LIKE escaping and free-text bounds | Info | Partial | #1006 | `331053c0` | Follow-up: escape the tag-suggestion prefix | |
| BE-28 | Merchant dictionary vote attribution | Info | Not fixed | | | Follow-up: recompute votes server-side, one per org | |
| BE-29 | User merge foreign-key coverage | Info | Fixed | #1008 | `ad1a989f` | | |
| BE-30 | Typed superadmin-guard errors | Info | Fixed | #1008 | `9885ea52` | | |
| BE-31 | `agent:auto` scope behaviour | Info | Accepted | | | By design: reversible writes without confirmation | |
| DB-06 | Step-up token storage and key fingerprint | Info | Partial | #1009 | `95d3bc67` | Follow-up: key the AI-key fingerprint with an HMAC | |
| INF-11 | `seed.py` in production images | Info | Fixed | #1007 | `412d4c33` | | |
| INF-12 | Legacy encrypted secrets in history | Info | Needs owner | | | Confirm the secrets in the legacy app spec were rotated | |
| INF-13 | Commit metadata emails | Info | Partial | | | Identity chosen and the assessment branches re-authored; older history keeps earlier addresses; delete the stale `chore/graphify-refresh-2026-10-15` branch | |
| INF-14 | `security.txt` | Info | Fixed | #1004 | `0d284c22` | Enable GitHub private vulnerability reporting; renew `Expires` yearly | |
| INF-15 | Dependency advisory audit | Info | Partial | #1007 | `f4b27914` | Decide on a `vite` override for dev-server advisories | |
| INF-16 | Settings outside this repo | Info | Needs owner | | | Add a minimum release age for uv in the shared Renovate preset | |

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
