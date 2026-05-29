# Hide remaining payment surfaces, ship baseline SEO, allow Ollama LAN IPs — design

**Status:** approved by operator 2026-05-29 (sections 1–3 + packaging).
**Date:** 2026-05-29.
**Source:** operator request 2026-05-29 — "I won't charge anything for now, given the complexity of this integration for a system that is not being fully used and validated by people other than me. Hide completely any payment references from the system and landing page." Plus baseline SEO request and the Ollama LAN IP rejection in the BYO credential form (screenshot 2026-05-29 13:26 GMT+2).

## Why one spec, three PRs

The three chunks are independent and ship separately. They are bundled in one spec because they were brainstormed together as a single session intent ("park monetization, draw users in, unblock my own LAN Ollama"). Each chunk has its own _Files_ + _Tests_ + _Rollout_ block; `writing-plans` should produce one phased plan with three independent phases (one PR per phase).

Packaging order (no dependency between PRs):

1. **PR A** — Ollama validator LAN fix (smallest, unblocks the screenshot scenario).
2. **PR B** — Extend `billing_ui_enabled` to the remaining payment surfaces.
3. **PR C** — Baseline SEO + admin-UI backlog file.

---

## Section 1 — Extend `billing_ui_enabled` to all remaining payment surfaces

### Goal

After this PR, with `BILLING_UI_ENABLED=false` (the production default since 2026-05-21), there must be zero user-visible reference to pricing, billing, plans, subscriptions, trials, or upgrades anywhere the operator points a non-superadmin user.

### Substrate (already exists from 2026-05-21)

The earlier spec [`2026-05-21-hide-billing-ui-until-payment.md`](2026-05-21-hide-billing-ui-until-payment.md) shipped:

- `BILLING_UI_ENABLED` env var, `app_settings.billing_ui_enabled: bool = False`.
- `/api/v1/auth/status` returns `billing_ui_enabled`.
- `AuthProvider` exposes `billingUiEnabled` via `useAuth()`.
- Already gates: `TrialBanner`, Settings/Billing tab in `SettingsLayout`, `/settings/billing` page, the `/app/page.tsx` trial-copy line.

This PR is a **coverage extension** of that same flag — no new flag, no rename. Same default (`false`), same `/auth/status` shape.

### Customer-facing surface (gap inventory, 2026-05-29 code survey)

Two categories of action, by render context:

**Hardcoded edits (landing surfaces — apex static export, flag is theatre)**

The apex deploy (`thebetterdecision.com`) is a **static export** that cannot read `/auth/status` at runtime. The 2026-05-21 spec set this precedent with Option A — hardcode the copy edit, revert via PR when payment is wired. This PR follows the same Option A for the remaining landing surfaces; the `billingUiEnabled` flag does not gate them because the static export can't read it.

| File | What's still visible | Action |
|---|---|---|
| `frontend/components/landing/PricingPreview.tsx` | Whole pricing section (Free / Pro / Team tiers, "Coming soon" badges, "Join the waitlist" CTAs, footer disclaimer about VAT). | **Delete** the file. Re-add via `git revert` when payment is wired. |
| `frontend/app/page.tsx` | `<PricingPreview />` import + render. | Remove the import and the `<PricingPreview />` tag from the page. |
| `frontend/components/landing/Faq.tsx` (item for "What payment methods will you accept?") | FAQ Q&A about payment methods. | **Delete** the entry from the FAQ array. |
| `frontend/components/landing/Faq.tsx` (item for "Is there a free plan?") | FAQ Q&A about the free plan. | **Delete** the entry from the FAQ array. |
| `frontend/components/landing/Faq.tsx` (line referring to "AI assistant (Pro tier)") | Inline "(Pro tier)" qualifier in a feature description. | **Edit** the line to strip "(Pro tier)" so it reads "optional AI assistant". |
| `frontend/components/landing/Hero.tsx` (or wherever the hero CTA lives — confirm at implementation) | No payment-clarity line near CTA. | **Add** a single line: _"Free while in beta. No credit card required."_ Hardcoded; revert separately if/when payment is wired and we want different copy. |

**Flag-gated edits (in-app + backend — read `billing_ui_enabled` at runtime)**

| File | What's still visible when flag is off | Action |
|---|---|---|
| `frontend/components/AppShell.tsx:178` | Admin nav item "Subscriptions" → `/admin/subscriptions` (CreditCard icon). | Filter from admin nav array when `!billingUiEnabled`. Same pattern as `SettingsLayout`'s existing tab filter. |
| `frontend/components/AppShell.tsx:184-188` | Admin nav item "Plan Catalog" → `/system/plans` (CreditCard icon). | Filter from admin nav array when `!billingUiEnabled`. |
| `backend/app/services/email_service.py:341-371` | `send_trial_expiring_email()` — periodic notification. | Wrap the **call site** (not the function) with `if app_settings.billing_ui_enabled:`. Keep the function intact so re-enabling is a config flip + the call-site guard removal. |

These three are runtime-flag-gated because they execute inside the FastAPI/Next.js runtime where `/auth/status` (frontend) or `app_settings` (backend) is available. Flipping `BILLING_UI_ENABLED=true` restores them without a code change.

### Files to touch

#### Frontend (hardcoded edits — landing)
- `frontend/components/landing/PricingPreview.tsx` — **delete file**.
- `frontend/app/page.tsx` — remove `PricingPreview` import + `<PricingPreview />` tag.
- `frontend/components/landing/Faq.tsx` — delete the two payment FAQ entries; strip "(Pro tier)" from the AI-assistant line.
- `frontend/components/landing/Hero.tsx` (confirm path during implementation) — add the "Free while in beta. No credit card required." line near the CTA.

#### Frontend (flag-gated — in-app)
- `frontend/components/AppShell.tsx` — wrap the two admin nav entries (Subscriptions, Plan Catalog) in a `billingUiEnabled &&` filter when assembling the admin nav array. Same pattern as `SettingsLayout`'s existing tab filter.

#### Backend (flag-gated)
- Wherever `send_trial_expiring_email` is invoked (likely a scheduled-task entrypoint such as `backend/app/services/notifications_scheduler.py` — confirm during implementation; the service module itself stays unchanged) — wrap the call site with `if app_settings.billing_ui_enabled:`.

### Tests

- **Frontend Vitest:** render test of landing page (`/`) asserting no "Pro" / "Team" / "€9" / "Coming soon" / "Join the waitlist" / "payment methods" strings appear. (Landing surfaces are deleted, not flag-gated — single-state test.) Render test of `AppShell` admin nav with `billingUiEnabled=false` (Subscriptions + Plan Catalog absent) and `billingUiEnabled=true` (both present).
- **Backend pytest:** test that the trial-expiring-email scheduled-task entrypoint does NOT invoke `send_trial_expiring_email` when `app_settings.billing_ui_enabled=False`. Mock the email transport and assert zero send attempts. Also test the inverse with the setting flipped.
- **No e2e.** Existing billing-page tests from 2026-05-21 cover the in-page render path.

### Rollout

This PR ships with `BILLING_UI_ENABLED=false` (already the prod default). Customer-facing payment surface disappears completely on merge.

To re-enable when payment is wired:

1. `git revert` the landing-page deletions/edits (PricingPreview file, page.tsx import, FAQ entries, hero copy line) — or rewrite the copy if the messaging has evolved.
2. Flip `BILLING_UI_ENABLED=true` in `.do/app.yaml` and any other environment surfaces (see `ENVIRONMENT.md`).
3. Remove the `if app_settings.billing_ui_enabled:` guard around `send_trial_expiring_email` call site (or leave it — it's permissive when the flag flips true).
4. Deploy. Admin nav links and the trial email come back automatically when the flag is true.

Rollback is symmetric: flip flag back to `false`.

### Out of scope

- Backend API gating of `/api/v1/subscriptions`, `/api/v1/plans`, `/api/v1/admin/subscriptions/*`. Same reasoning as 2026-05-21: owner-only auth, no UI calls them with flag off. Defense-in-depth not worth the rollback complexity.
- `OnboardingPageBody.tsx:373` "billing" mention — confirmed in 2026-05-21 review as transaction-billing, not subscription-billing.
- Removing Paddle backend integration, billing models, plan-catalog service, etc. All dormant when no UI consumes them.

---

## Section 2 — Baseline SEO + admin-UI backlog file

### Goal

Make the marketing landing page and the public auth pages indexable and discoverable for terms like "personal finance app", "budget planner", "self-hosted finance", etc. Avoid leaking auth-walled routes into search results. Defer the per-route admin SEO config UI to a follow-up backlog item.

### Substrate (already exists, 2026-05-29 inventory)

- `frontend/app/layout.tsx:12` — root metadata: title template `%s · The Better Decision`, default title + description, robots stub, OG/Twitter stubs.
- `frontend/app/page.tsx:23` — landing page already calls `pageSocialMeta()` for OG + Twitter cards + canonical.
- `frontend/app/robots.ts` — allow `/`, `/login`, `/register`, `/privacy`, `/terms`, `/forgot-password`; disallow `/dashboard/*`, `/auth/*`, `/api/*`; references sitemap.
- `frontend/app/sitemap.ts` — 5 URLs (`/`, `/register`, `/login`, `/privacy`, `/terms`) with priorities + lastModified.
- `frontend/app/opengraph-image.tsx` — dynamic 1200×630 OG image generator.
- `frontend/app/icon.svg` — favicon.
- Landing page already renders one `application/ld+json` block with `@type: SoftwareApplication` (CSP-nonced).

### Indexability matrix

Crawler should see (`index, follow`): `/`, `/login`, `/register`, `/privacy`, `/terms`, `/docs`, `/docs/plans`.

Crawler should NOT see (`noindex, nofollow`): all auth-walled routes under the app shell **plus** the public-but-not-useful pages `onboarding`, `accept-invite`, `forgot-password`, `reset-password`, `verify-email`, `mfa-verify`. These are flow pages tied to a session or a per-org token — there's no SEO value in indexing them, and indexed reset-password URLs are a footgun.

### Gaps to close

1. **`noindex` on auth-walled routes.** 19 routes under the app shell have no per-page `robots` directive. `robots.ts` already disallows them at the crawler level, but engines occasionally index discovered URLs without a per-page directive. Add one `export const metadata = { robots: { index: false, follow: false } }` at the **shallowest shared layout** of the authenticated app (likely `frontend/app/(app)/layout.tsx` or whatever route group wraps the auth-walled pages — confirm at implementation). One file, not nineteen.
2. **`noindex` on flow-only public routes.** Add the same `metadata.robots` directive to `onboarding`, `accept-invite`, `forgot-password`, `reset-password`, `verify-email`, `mfa-verify`. Per-route (no shared layout to lean on).
3. **Sitemap expansion.** Add `/docs` and `/docs/plans` to `frontend/app/sitemap.ts`. Keep priorities low (0.4) — support content, not conversion. Do NOT add the flow pages to the sitemap.
4. **Landing `<h1>`.** Verify the Hero component has exactly one `<h1>`. If absent or generic, replace with a keyword-rich phrase. Starter copy (operator finalizes at implementation): _"Personal finance, planned not panicked."_ If multiple `<h1>`s exist on the page, demote extras to `<h2>`.
5. **Per-page meta on indexable public pages.** Each of `/`, `/login`, `/register`, `/privacy`, `/terms`, `/docs`, `/docs/plans` should have a distinct, keyword-relevant `title` + `description`. The inventory confirmed each has *some* metadata — verify each is distinct (not just inheriting the root template) and tuned for the page's intent. Fill in any that just inherit.
6. **JSON-LD enhancement on landing.** Extend the existing `SoftwareApplication` block in `frontend/app/page.tsx` to include `author` and `publisher` properties. Add a sibling `FAQPage` block that mirrors the (post-Section-1) FAQ section — only non-payment FAQs end up in the structured data. Skip `aggregateRating` until real reviews exist (Google penalizes fake review markup).
7. **Backlog file.** Write `specs/seo-admin-config-backlog.md` (one screen, ≤200 words) describing the future admin UI: per-route DB-stored title/description/keywords/OG-image override, superadmin-only, fed via a new `SeoOverride` model that takes precedence over hardcoded `metadata` exports. No code, just intent + sketched data model — starting point for when the operator picks this up.

### Out of scope this PR

- Admin UI for SEO config (deferred via backlog file).
- `hreflang` (single-language site for now).
- Custom OG image per route (single shared dynamic OG is fine for v1).
- Schema.org `Review` / `aggregateRating` (no real reviews to cite).
- Backend-served sitemap (Next.js `sitemap.ts` is enough; not enough URLs to justify a DB-backed sitemap yet).
- Performance/Core Web Vitals work (separate concern, not SEO baseline).

### Files to touch

- `frontend/app/(app)/layout.tsx` (or equivalent shared layout for the auth-walled group — confirm at implementation; create one if none exists) — `export const metadata = { robots: { index: false, follow: false } }`.
- `frontend/app/onboarding/layout.tsx` (or `page.tsx`), `frontend/app/accept-invite/...`, `frontend/app/forgot-password/...`, `frontend/app/reset-password/...`, `frontend/app/verify-email/...`, `frontend/app/mfa-verify/...` — same `metadata.robots` noindex directive on each.
- `frontend/app/sitemap.ts` — append `/docs` and `/docs/plans` URLs.
- `frontend/components/landing/Hero.tsx` (confirm path at implementation) — `<h1>` audit + tune.
- Per-page `metadata` exports for the 7 indexable public pages — verify + fill where needed.
- `frontend/app/page.tsx` — extend the existing JSON-LD render to include `author`/`publisher` on `SoftwareApplication` and to add a `FAQPage` block.
- `specs/seo-admin-config-backlog.md` — new file, see Gap #7.

### Tests

- Fetch-based test: `GET /robots.txt` returns 200 with the expected allow/disallow rules; `GET /sitemap.xml` returns 200 and includes `/docs` and `/docs/plans` plus the original 5 URLs.
- Vitest render test on `/`: assert exactly one `<h1>`; assert two `<script type="application/ld+json">` blocks (SoftwareApplication + FAQPage); assert `FAQPage` JSON includes the post-Section-1 surviving FAQ entries (and excludes the two deleted payment ones).
- Vitest render-head test on `/dashboard` (or any auth-walled route): assert `<meta name="robots" content="noindex, nofollow">` is present.
- Vitest render-head test on `/forgot-password` (sample of the flow-only public list): assert `<meta name="robots" content="noindex, nofollow">` is present.
- Vitest render-head test on `/login` (sample of the indexable list): assert `noindex` is **absent**.

### Rollout

Ship. SEO baseline is invisible to current users; only crawlers notice. Verify post-deploy with `curl https://thebetterdecision.com/robots.txt` and `…/sitemap.xml`, and check Google Search Console once indexed.

---

## Section 3 — Ollama validator: allow private/LAN IPs

### Goal

When the operator configures an Ollama credential, allow `base_url` to be a literal RFC1918 LAN IP (e.g., `http://192.168.1.163:11434/`) or loopback (`http://127.0.0.1:11434/`) without breaking the SSRF guard against cloud metadata + link-local addresses, and without weakening the guard for other providers.

### Substrate

- `backend/app/schemas/org_ai_credential.py:29-69` — `_reject_private_ip_literal(host)` blocks loopback, RFC1918, link-local, multicast, unspecified, reserved, IPv4-mapped IPv6, and cloud metadata IPs.
- `_validate_base_url(value)` at L72-87 runs `_reject_private_ip_literal` unconditionally inside a Pydantic `@field_validator`, which doesn't have access to the `provider` field.
- The `@model_validator(mode="after")` block at L110-130 already runs provider-aware checks (api_key requirements, bearer_token validity per provider). This is where the provider-conditional IP policy belongs.

### Behavior change

For **`provider == OLLAMA`**: allow RFC1918 (`10/8`, `172.16/12`, `192.168/16`) and loopback (`127.0.0.0/8`, `::1`). Continue blocking:

- Cloud metadata: `169.254.169.254`, `fd00:ec2::254` (and IPv4-mapped IPv6 variants).
- Link-local (the rest of `169.254/16` beyond the metadata constant — IMDS bypass surface).
- Multicast / unspecified / reserved.

For **all other providers**: behavior unchanged. Full strict block per the current `_reject_private_ip_literal`.

### Code shape

Refactor the IP guard into two functions:

```python
def _reject_metadata_or_unsafe(host: str) -> None:
    """Always-blocked classes: metadata IPs, link-local, multicast,
    unspecified, reserved. Safe for all providers including Ollama."""

def _reject_private_or_loopback(host: str) -> None:
    """RFC1918 + loopback. Blocked for hosted providers, allowed for
    Ollama (operator's own LAN/homelab)."""
```

Move BOTH calls out of `_validate_base_url` and into a new model-validator helper invoked from `_check_provider_requirements`:

- Field validator `_check_base_url` keeps the scheme + hostname-presence checks and unconditionally runs `_reject_metadata_or_unsafe(host)`.
- Model validator (post-validation, when `provider` is known) runs `_reject_private_or_loopback(host)` only for non-Ollama providers.

The `OrgAICredentialRotate` schema does not include `base_url`, so it needs no change.

### Files to touch

- `backend/app/schemas/org_ai_credential.py` — refactor as above; update the `_validate_base_url` docstring to reflect the provider-conditional policy.

### Tests

`backend/tests/test_org_ai_credential_schema.py` (extend the existing tests; the file already covers happy paths from the 2026-05-22 BYO credentials work):

| Case | Provider | base_url | Expected |
|---|---|---|---|
| LAN IP for Ollama | `ollama` | `http://192.168.1.163:11434/` | accept |
| Loopback IPv4 for Ollama | `ollama` | `http://127.0.0.1:11434/` | accept |
| Loopback IPv6 for Ollama | `ollama` | `http://[::1]:11434/` | accept |
| Metadata IP for Ollama | `ollama` | `http://169.254.169.254/` | reject (metadata) |
| Link-local for Ollama (non-metadata) | `ollama` | `http://169.254.1.1/` | reject (link-local) |
| Multicast for Ollama | `ollama` | `http://224.0.0.1/` | reject |
| LAN IP for openai_compatible | `openai_compatible` | `http://192.168.1.163/` | reject (unchanged) |
| IPv4-mapped IPv6 LAN for Ollama | `ollama` | `http://[::ffff:192.168.1.1]/` | accept |
| IPv4-mapped IPv6 metadata for Ollama | `ollama` | `http://[::ffff:169.254.169.254]/` | reject (metadata via mapped) |
| Public IP for any provider | `ollama` / `anthropic` | `https://api.example.com/` | accept (unchanged) |

### Frontend

No change. The error from the backend is already surfaced inline in the form (the screenshot proves it). With the validator fix, the user's exact scenario passes silently.

### Rollout

Ship in its own PR. Backwards-compatible (the only behavior change is *accepting* previously-rejected inputs, never rejecting previously-accepted ones). No env var, no migration, no flag.

### Out of scope

- DNS-rebinding protection (custom httpx transport that re-resolves at connect time). Already noted as a residual v1 risk in the existing docstring (L36-40). Not regressed by this change.
- A future opt-in "trust-the-operator" mode that allows private IPs for `openai_compatible` too. The model-validator structure makes this trivial to add later by widening the conditional from `provider == OLLAMA` to `provider in {OLLAMA, OPENAI_COMPATIBLE} and app_settings.ai_trust_local_endpoints`.

---

## Cross-cutting notes

- **No new env vars.** Section 1 reuses `BILLING_UI_ENABLED`. Sections 2 + 3 introduce none.
- **No DB migrations.** All three sections.
- **No new dependencies.**
- **Memory links:** [[reference_do_spec_sync]] — not invoked here (no new env vars), but the rule still governs any future flag flip. [[feedback_pr_format]] — PRs land without test-plan sections. [[feedback_no_push_main]] — three branches + three PRs.
- **No backwards-compat shims** (per [[feedback_pre_launch_state]]) — Section 1's PricingPreview deletion is hard-remove; revert via `git revert` when payment is wired.
