# Apex Analytics Consent (Consent Mode v2 + banner)

**Date:** 2026-06-20
**Status:** Approved, in implementation
**Author:** brainstorm session (operator: flamarion@fjconsulting.io)

## Problem

GA4 loads unconditionally on the apex marketing site (`thebetterdecision.com`).
`frontend/components/analytics/GoogleAnalytics.tsx` fires `gtag('config', …)` on
every page load, setting `_ga` / `_ga_*` cookies and sending a `page_view` to
Google **before any user interaction**. There is no cookie banner, no CMP, and
no Google Consent Mode anywhere in the codebase. The privacy policy documents an
**opt-out** model (Google's opt-out browser add-on), which is not valid consent.

This violates Google's EU User Consent Policy and the EU ePrivacy Directive +
GDPR for EEA/UK visitors: non-essential analytics cookies require **prior
opt-in** consent, and that consent must be passed to Google via Consent Mode v2.
The operator is in the EEA (NL) and the site serves EU traffic.

Confirmed direction (brainstorm): **add Google Consent Mode v2 with default-denied
storage plus an Accept / Reject / Customize consent banner.**

## Goals

1. No GA cookies or analytics collection before opt-in consent for EEA/UK users.
2. Consent Mode v2 wired so Google receives the consent state (default-denied,
   updated on the user's choice).
3. A banner with equally-weighted Reject / Customize / Accept and per-category
   toggles (Necessary, Analytics, Marketing).
4. A persistent way to change or withdraw consent (footer link), and periodic
   re-consent (6 months).
5. Privacy policy updated from opt-out to the opt-in reality.

## Non-Goals

- Consent on the authenticated app host. GA never runs there; the app sets only
  necessary cookies (refresh token, theme, bot-management). No banner on the app.
- Activating Marketing/Ads. The Marketing toggle is wired to the Consent Mode ad
  signals but stays unused until ads launch; no ad tags load and the CSP is NOT
  widened for ad endpoints in this work (follow-up at ads launch — see below).
- A third-party CMP / consent library. Custom, since we have one analytics
  category in active use and a strict static-export CSP.
- Server-side consent storage. The apex is a static export with no request-time
  runtime; consent lives in `localStorage`.

## Decisions

- **Decision 1 — banner model is Reject / Customize / Accept**, three
  equally-weighted actions (GDPR: rejecting must be as easy as accepting; no
  pre-ticked boxes, no consent wall). Customize expands per-category toggles.
- **Decision 2 — categories are Necessary (locked on) + Analytics + Marketing.**
  Marketing is shown now to future-proof for the operator's parked Ads credit;
  having unused consent carries no GDPR risk (the risk is the reverse). It maps
  to the ad Consent Mode signals but is inert until ads launch.
- **Decision 3 — consent persists in `localStorage`, re-asked after 6 months.**
  Conservative end of CNIL/ICO guidance (6–13 months). Versioned key so a future
  category change can invalidate stored choices.
- **Decision 4 — apex-only.** The banner and Consent Mode are gated on
  `isApexBuild`, matching GA's scope.
- **Decision 5 — custom banner, no new CSP origins.** `script-src` already
  allows `'unsafe-inline'`, which covers the inline Consent Mode default script.

## Architecture

### Consent Mode v2 default (the core mechanism)

`GoogleAnalytics.tsx` renders, in this `dataLayer` order, BEFORE the async
gtag.js loader's effect and before `config`:

```js
window.dataLayer = window.dataLayer || [];
function gtag(){dataLayer.push(arguments);}
gtag('consent', 'default', {
  ad_storage: 'denied',
  ad_user_data: 'denied',
  ad_personalization: 'denied',
  analytics_storage: 'denied',
  personalization_storage: 'denied',
  functionality_storage: 'granted',
  security_storage: 'granted',
  wait_for_update: 500,
});
// Re-apply a previously-stored, non-expired choice synchronously so returning
// consenters are measured on first paint:
//   read localStorage 'tbd-consent-v1'; if valid -> gtag('consent','update', …)
gtag('js', new Date());
gtag('config', 'G-GRXDVTVBLV');
```

The stored-consent re-application is generated inline from a single source of
truth (see the helper below) so the inline script and the React banner can never
diverge. With `analytics_storage` denied, GA4 runs in cookieless modeled mode
(no `_ga` cookies, pings carry `gcs`/`gcd` consent signals) until granted.

### Consent helper — `frontend/lib/consent.ts`

Pure, framework-free, unit-testable. Single source of truth for the storage
shape and the Consent Mode mapping. No DOM or React imports.

- `CONSENT_STORAGE_KEY = 'tbd-consent-v1'`
- `CONSENT_TTL_MS = 1000*60*60*24*182` (~6 months)
- `type ConsentChoice = { analytics: boolean; marketing: boolean; ts: number }`
- `readConsent(now): ConsentChoice | null` — parse + validate + expiry check;
  returns null on missing / malformed / expired.
- `writeConsent(choice)` — persist with timestamp.
- `toConsentModeUpdate(choice): Record<string,'granted'|'denied'>` — maps
  `analytics → analytics_storage`; `marketing → ad_storage + ad_user_data +
  ad_personalization`; necessary types always granted.
- `DEFAULT_DENIED` — the default-denied object used by the inline script.
- A `gtagConsentUpdate(choice)` thin wrapper that calls `window.gtag('consent',
  'update', toConsentModeUpdate(choice))` (guarded if gtag absent).

### Banner — `frontend/components/landing/ConsentBanner.tsx` (client component)

- SSR-safe: returns `null` on the server / first render; decides visibility in a
  `useEffect` after reading `readConsent(Date.now())`. No hydration mismatch on
  the static export.
- Visible when no valid stored choice; hidden otherwise.
- Collapsed view: short copy + Privacy Policy link + **Reject · Customize ·
  Accept** (equal visual weight).
- Customize expands toggles: **Necessary** (checked, disabled), **Analytics**,
  **Marketing**, with a **Save preferences** action.
- Accept → `{analytics:true, marketing:true}`; Reject → `{false,false}`;
  Save → toggle states. Each action: `writeConsent(...)` then
  `gtagConsentUpdate(...)`, then hide.
- Exposes a way to be re-opened (see footer link). A module-level event or a
  small context: the footer "Cookie preferences" button dispatches a custom
  event (`tbd:open-consent`) the banner listens for and re-shows itself,
  independent of stored state.
- Mounted once in the apex layout (gated on `isApexBuild`).

### Footer link

The apex/landing footer gains a **"Cookie preferences"** button that dispatches
`tbd:open-consent`. Satisfies "withdraw as easily as you consented." Present
whenever GA is in play (apex).

### Consent → Consent Mode mapping

| Toggle | Consent Mode signals |
|---|---|
| Necessary (locked) | `functionality_storage`, `security_storage` = granted |
| Analytics | `analytics_storage` |
| Marketing | `ad_storage`, `ad_user_data`, `ad_personalization` |

### Privacy policy update (`frontend/app/privacy/page.tsx`)

Replace the opt-out paragraph (Google opt-out add-on) with: GA4 runs only after
opt-in consent via a banner; Consent Mode v2 defaults to denied; the
`tbd-consent-v1` localStorage entry stores the choice; users change/withdraw via
the footer "Cookie preferences" link; re-asked every 6 months. Add the consent
localStorage entry to the cookies/storage list.

## Testing

- **Unit (`lib/consent.ts`):** read/write round-trip; expiry boundary (just
  under / just over 6 months); malformed-JSON and wrong-version → null;
  `toConsentModeUpdate` mapping for all four combinations; `DEFAULT_DENIED`
  shape.
- **Component (`ConsentBanner.tsx`):** hidden when a valid choice exists; shown
  when absent or expired; Accept / Reject / Save each persist the right choice
  and call `window.gtag` with the correct `consent`/`update` payload; the
  `tbd:open-consent` event re-opens it regardless of stored state; Necessary
  toggle is disabled+checked.
- **Inline script (`GoogleAnalytics.tsx`):** the rendered inline script contains
  a `consent`/`default` all-denied call positioned before `gtag('config'`.
- Full `vitest run` + `eslint . --quiet` + `tsc --noEmit` before the PR (CI
  gates on all three).

## Backward Compatibility / Rollout

- Behavior change is intended: EEA/UK visitors are no longer tracked pre-consent.
- No env or infra change. Apex CSP unchanged (`'unsafe-inline'` covers the inline
  consent script; no ad origins added).
- Apex deploy auto-triggers — `frontend/components/analytics/**`,
  `frontend/lib/**` (consent helper), and the landing components/privacy page are
  all in the `apex-deploy.yml` paths filter.

## Follow-ups (out of scope, tracked)

- **At ads launch:** acting on granted Marketing consent will require loading the
  relevant Google ad tags and widening the apex CSP for the ad endpoints
  (`stats.g.doubleclick.net`, the `ga-audiences` ccTLD pixels) and/or enabling
  Google Signals. Until then the Marketing toggle is inert.
- Reports usefulness Phase 2 (tooltip + quick-add) and Phase 3 (templates) remain
  queued; Phase 2 table per-column source was found already shipped.
