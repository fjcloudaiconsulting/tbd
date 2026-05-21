---
name: System Architect Review 2026-05-02
description: External architect review of PFV — captures roadmap-level recommendations to apply post-L3.10. Also records what the team explicitly disagreed on and why.
type: project
---
## Context

External system architect reviewed the PFV codebase, status, roadmap, smart-rules spec, billing-flow notes, and test backlog on **2026-05-02** (during L3.10 implementation). Net assessment: PFV is no longer a prototype — it has a "fairly serious SaaS skeleton." The biggest 1.0 risk is **product completeness around import, onboarding, trust, and operations**, not core code structure.

**Why:** Sanity check before scope balloons further. Architect was specifically asked to flag risks where the team has been engineering-strong and product-thin.

**How to apply:** Treat this as the next-backlog list once L3.10 ships. NONE of these are in scope for the current `feat/smart-rules-categorization` branch.

---

## In-flight (applied during L3.10)

| Item | Decision | Action taken |
|---|---|---|
| Tougher normalization tests + restore spec's "longest letter-run" tokenization | **Applied** | Plan Task 2 expanded with ~13 messy real-world descriptors (IBAN tails, double terminal IDs, double dates, accent folding, URL-ish merchants) and the implementation now does explicit token-walking, not just regex chains. |
| Conservative learning ("don't overwrite high-match rules on a single edit") | **Deferred** | Stay with most-recent-wins per locked spec. Fjorge's stance: user picks are commands, not AI suggestions; the opposite failure mode ("I keep editing this and it won't stick") is worse. Architect agreed: "Do not change learning semantics yet. Adds policy before evidence." Revisit after metrics show whether sticky bad rules are real. |
| Smart-rules metrics from day one | **Applied (with architect's refinement)** | Plan Tasks 6/7 now emit `smart_rules.preview_built` (one aggregate per preview) and `smart_rules.import_executed` (one aggregate per import). `smart_rules.miss` fires per UNIQUE normalized token within an import, NOT per row — keeps "top missed tokens" answerable later without log spam. Privacy: `org_id` and `normalized_token` only; raw description never logged. |

---

## Post-L3.10 backlog (the architect's pull-forward list)

Order is rough priority. Sequencing details belong in `project_roadmap.md` once L3.10 ships and we re-plan Week 7+.

### Trust + product completeness (cheap, launch-relevant)

1. **Uncategorized Inbox** — focused view for transactions with no category. Pairs with smart rules. Daily workflow: "clean up what the importer couldn't classify." High user value, ~2 day build.
2. **Import Quality Dashboard** — after each import, show: imported count, skipped duplicates, transfers detected, uncategorized count, suggestions accepted. Turns import from black box into transparent step. Reuses the metrics scaffold from L3.10. ~1-2 day build.
3. **Data Export** (GDPR portability) — currently in P1; pull forward. Simple ZIP with CSV/JSON. ~1 day. Trust-relevant for finance.
4. **Account Closure / Delete My Data** — even if v1 is "request deletion" via email + admin tool. ~1 day. Trust-relevant.
5. **Opening Balance UX** — already on pentest follow-ups. Either reject non-zero balance at create OR auto-create opening-balance transaction atomically. Architect prefers the latter (better UX). Decide before opening the PR.
6. **Global error / empty / loading states** (L5.7) — pull forward. Public products feel fragile when framework defaults leak. Add empty states for transactions, budgets, forecasts, imports.
7. **First-Run Setup Wizard** (L3.3) — short flow: create accounts, choose/import categories, set period dates, optionally seed demo data, then import. More important for conversion than several advanced analytics features.

### Sequencing the architect explicitly called out

8. **L4.7 Audit log before more admin power.** L4.3 already introduced destructive admin ops with structlog events. Persist them before adding impersonation, user management actions, feature overrides, manual verification, or payment adjustments. Treat L4.7 as a near-term platform primitive, not a later admin nice-to-have.
9. **L4.11 Plan features & org overrides BEFORE any LAI work.** Already locked in roadmap (Week 8). Architect is just confirming the sequencing — don't ship Pro positioning, AI plan columns, or per-org comps until L4.11 lands.
10. **LAI.1 ONLY after smart-rules coverage is measured**, then **LAI.5 usage caps before any broader AI surface**. Don't rush the LLM tier just because the column exists.

### Operational observability for support (paid-launch gate, not public-beta)

11. Request correlation, auth context, user/org drilldown, persisted admin events. L4.7 + L4.9 are the backbone. Without them, support keeps requiring raw DB / log spelunking. Acceptable gap for the friend-tester phase; not acceptable once paid.

### Help / support surface

12. **Help/Manual focused on workflows**, not generic docs. Topics: "import bank CSV", "fix uncategorized transactions", "set billing cycle", "understand forecast vs plan", "reset demo data".
13. **User-Visible Security Activity** — login history, active sessions, MFA changes, password changes. Post-launch nice-to-have; reinforces trust + helps support.
14. **Admin Support Timeline** — per-org timeline of imports, subscription changes, admin actions, member invites, resets, exports. Backed by L4.7 audit events. Builds on item 8.

### The architectural slow-burn (most important and most underweighted)

15. **Billing period model is at risk of becoming a UX trap.** Schema supports explicit period dates, but if the UI stays cycle-day-driven, users with salary-anchored periods, multi-card cycles, or provider spillover will fight the app. Budget vs Forecast 1.0 (PR #100) started fixing this. The next time we touch a period-aware screen, drop the "current period = calendar month" assumption end-to-end. Doesn't ship a feature; changes how every screen thinks. The most subtle and most important point in the review.

---

## Architect's launch-gate framing (advisory, not adopted as ceremony)

The architect proposed three explicit gates: Public Beta / Paid Launch / Pro Differentiation.

**Decision:** **don't formalize gate names.** PFV is one person's side project being tested by EU friends. Naming gates implies process. Track sequencing in the roadmap and project_status; that's enough. The implicit version of the architect's framing is already in place: friends now (L3.10 + L1.4-L1.6 security finish + this backlog), charge when L2 + L4.11 + L4.7 + closure flow are all done, AI tier last.

**What to take from the framing anyway:** the prioritization signal — security-finish (L1.4/L1.5/L1.6) and import-hardening should rank higher than they currently do, because they're cheap and trust-visible.

---

## Changelog

- **2026-05-02** — Initial capture during L3.10 implementation. A + C applied in plan; B deferred; D (this file) recorded as next-backlog reference.
