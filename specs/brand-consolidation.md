---
name: Brand consolidation (pre-1.0 launch) — PARTIAL
description: Standardize naming on "The Better Decision" + product logo. Customer-facing surfaces mostly shipped (#125, #224, #228, #229, #230, #231). Internal/operational rename (repo, working dir, CLI script, compose services, DB name, env vars, memory slug) still open; product-logo asset set may also have residual scope beyond #224's foundation pass. Architect decision pending on internal rename scope.
type: project
originSessionId: 497b4e5b-526f-4ed1-bca0-0ee0c5bbc716
---
**STATUS: 🟡 PARTIAL.** Customer-facing brand work shipped through multiple PRs:
- **PR #125** (L5.7): brand-consistent error / 404 / loading fallbacks.
- **PR #224** (L5.10): brand foundation assets, tokens, voice guide.
- **PR #228**: localStorage key rename `pfv2-theme` → `tbd-theme`.
- **PR #229** (L5.9): brand-compliant Google SSO button.
- **PR #230** (L5.1): landing brand integration.
- **PR #231** (L5.6): brand-aligned email templates.

**Still open:** internal/operational rename scope (repo `flamarion/pfv`, working dir `/Users/fjorge/src/pfv`, `./pfv` CLI → `./tbd`, compose service names, DB name `pfv2`, env var prefixes, Claude memory slug). Architect to decide full-rename vs grandfather, then scope and dispatch.

Original 2026-05-08 capture below preserved for context.

## Why
Today the name splits: "The Better Decision (TBD)" in customer-facing surfaces (README, in-app copy, public `/docs` route); "Personal Finances V2" / "pfv" / "pfv2" in repo name, working directory, container names, DB, CLI script, env var prefixes, internal docs. Mixed naming creates friction at launch (onboarding / legal copy / support channels) and in agent prompts (which name is canonical?). Decision: **The Better Decision** is the canonical product name. **TBD** acceptable as short form in internal/casual contexts.

## Two parts

### 1. Naming standardization

**User-facing surfaces (non-negotiable):**
- In-app copy: page titles, header, footer, error messages, empty-state copy
- Email templates (Mailgun)
- Legal pages (`/privacy`, `/terms`)
- Public `/docs` route content (added in PR #159)
- README.md, CONTRIBUTING.md (already use "The Better Decision" — double-check no stragglers reference pfv as a product name vs. as a repo/dir name)
- Marketing surface (if any) — domain, landing page, OG image, social share

**Internal/operational surfaces (decision needed: full rename vs. grandfather):**
- GitHub repo: `flamarion/pfv` → e.g. `flamarion/the-better-decision` or `flamarion/tbd` (renames preserve issue/PR history; GitHub auto-sets up redirects)
- Local working dir: `/Users/fjorge/src/pfv` → match
- CLI script: `./pfv start|stop|...` → `./tbd` (or a wrapper aliasing the old name during transition)
- Docker compose service names: `pfv-backend`, `pfv-frontend`, `pfv-mysql`, `pfv-redis`, `pfv-nginx`
- DB name: `pfv2` → decide
- Env var prefixes (if any `PFV_*` exist) → decide
- Claude memory dir slug: `~/.claude/projects/-Users-fjorge-src-pfv/` is derived from the working directory. Renaming the dir creates a new slug; old memory becomes orphaned. Plan a one-time copy.

### 2. Logo design

**Asset set required:**
- SVG primary logo / wordmark (app header + marketing)
- Favicon (16, 32, 48, 180 px PNG + .ico fallback)
- OG image (1200×630 — social share)
- App icon (512×512 + maskable variant if PWA-installable later)
- Email header image (Mailgun templates)

**Direction TBD:**
- Type-only wordmark vs. wordmark + symbol vs. symbol-only
- Color palette: pick 2-3 brand colors that survive light/dark mode (Tailwind-driven theme today)
- DIY (system-stack wordmark + one geometric mark) likely sufficient for 1.0; commissioning is upside, not a launch dependency

## Constraints

- **Repo rename** triggers: GitHub redirects (auto), DO App Platform `spec.git_source.repository` update (manual), GitHub Actions workflow secrets / OIDC trust if any, working-tree remote URLs, README badges.
- **DB rename**: per `feedback_pre_launch_state.md`, no production data exists yet. A fresh DB on the next deploy is acceptable; no migration tool needed.
- **Mailgun sender identity / DKIM** may reference current naming — verify what changes if anything.
- **Claude memory slug rename** is a one-time chore (copy old → new slug, then delete old).

## Suggested PR shape (architect to confirm)

If full rename is chosen:
- **PR 1** in-codebase rename: CLI script, compose service names, env var prefixes, DB name in compose, README/CONTRIBUTING housekeeping, customer copy sweep.
- **PR 2** repo rename: GitHub UI action + Actions workflow updates + DO `spec.git_source` update + working-tree remote re-setup.
- **PR 3** logo assets + frontend wiring (favicons, OG image, header logo, email template).

If internal `pfv` is grandfathered:
- **Single PR** customer copy sweep — mechanical, fast.
- Logo PR is unchanged.

## Open questions

- **Domain** for the launched product (e.g. `thebetterdecision.app` / `.com`) — drives marketing surface and email sender choice.
- **Trademark availability** on "The Better Decision" — quick search before committing.
- **Internal `pfv` rename**: full sweep or grandfathered? Architect to weigh cost (operational churn, broken bookmarks, redirect debt) against long-term clarity.
- **Logo direction**: dispatched to a creative agent? DIY by you? External commission?

## Status update 2026-05-14

Both brand parts have landed:

**Domain locked.** `thebetterdecision.com` is live as the apex (AWS S3 + CloudFront, L5.2a, PRs #240/#241/#267/#270/#271). `app.thebetterdecision.com` continues to host the authed app (DO App Platform). Email sender is `hello@thebetterdecision.com` (constant in `frontend/lib/brand.ts`).

**Internal `pfv` is grandfathered.** No repo / DB / CLI / compose-service rename has been performed. The brief Brand-team dispatch (2026-05-14, this memo's Section C Team 1) explicitly held that scope: "DO NOT rename repo/DB/internal pfv unless explicitly approved." Locked.

**Logo direction locked: DIY type-only-plus-mark.** The decision was made in PR #224 (`feat(brand): foundation assets, tokens, voice guide (L5.10)`). The mark is two stacked chevrons reading as a decision arrow (the lower a muted slate echo, the upper brass) — "no best, only better." Implemented in `frontend/components/brand/Logo.tsx` as `<Mark />` / `<Wordmark />` / `<Logo />`. Brand surface palette in `frontend/lib/brand.ts`. Full kit documented in `BRAND.md` at repo root.

**Asset surface shipped:**
- `frontend/app/icon.svg` — 32px favicon (live).
- `frontend/app/apple-icon.tsx` — 180×180 generated PNG for iOS (live).
- `frontend/app/opengraph-image.tsx` — 1200×630 generated PNG for social share (live).
- Email header: inline chevron mark in `backend/app/services/email_service.py`, copied verbatim from `icon.svg` so the email surface stays in lockstep (live, PR #231).
- AppShell + LandingFooter + TopNav use the canonical `<Logo />` component.

**Voice policy:** customer-copy em-dash sweep landed today (PR #273 from `feat/brand/voice-sweep`), plus regression-guard test at `frontend/tests/voice/no-em-dash-in-customer-copy.test.ts`.

**Landing iteration (L5.1):** added `HowItWorks` 3-step section + Hero trust line (PR #279 from `feat/brand/landing-iteration`). Apex static export verified.

**Still open:**
- `favicon.ico` binary file (only `icon.svg` exists today). Build-apex allowlist already accepts it. Low priority — every modern browser honors the SVG favicon.
- 512×512 maskable PWA icon variant. Only needed when the app becomes PWA-installable (post-launch).
- Email-header PNG asset. Not needed today because every brand email renders the chevron inline; the inline SVG is the spec.

Logo / favicon / OG / email / landing iteration are all DONE at the code level. The remaining brand-consolidation backlog is post-launch polish, not blocking 1.0.
