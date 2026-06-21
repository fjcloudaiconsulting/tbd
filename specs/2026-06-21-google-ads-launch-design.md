# Google Ads launch — design & runbook (2026-06-21)

Status: **design approved by delegation** (operator trusts the Ads calls; engineering goes through normal PR self-review). One small engineering PR + an operator console runbook.

## Goal

Spend the parked **~€400 Google Ads credit** to put the product in front of *real strangers* (not operator + spouse) and learn which positioning pulls them in. Awareness-first; **not** a measurement project. At this scale the operator is superadmin and sees every signup in the admin user list, so completed-signup attribution is free — we only need a lightweight *intent* signal to let Google bid toward people who act.

Decisions taken during brainstorming:
- **Goal:** spend the credit to attract real users; landing-page traffic is valuable on its own (C, reframed).
- **Measurement floor:** one cheap **apex-only "clicked Register" conversion** (A). No server-side Measurement Protocol, no in-app GA.
- **Geo:** **Europe-wide, English** (B) — avoids brutal US/UK CPCs, matches the EUR/EU-consent reality.
- **Campaign type:** **Search** (operator-recommended default) — intent-driven, controllable, budget-respecting. Not Performance Max, not Display.
- **Positioning:** **A — differentiator + competitor terms** (cash-flow forecasting, what-if scenarios, "<competitor> alternative"). Not broad category terms ("budgeting app") — too expensive, lukewarm, Mint/YNAB/Monarch own them.

## Two tracks

1. **Engineering (one PR):** fire a GA4 `register_click` event when a visitor clicks any signup CTA on the apex. This is the *only* code change.
2. **Campaign (operator, in Google consoles):** link GA4↔Ads, import `register_click` as a conversion, build the Search campaign from the blueprint below. Fully specified here; no code.

---

## Engineering design — the `register_click` conversion

### Why a GA4 event imported to Ads, not a native Google Ads tag

| | GA4 event → import (CHOSEN) | Native Google Ads tag |
|---|---|---|
| Code | one `gtag('event', …)` call via existing tag | add `AW-…` config + conversion snippet |
| CSP | **no change** (uses existing GA `/g/collect` origins) | must allowlist `googleadservices.com`, `doubleclick.net` |
| Consent | **Analytics toggle only** (already wired) | needs the dormant **Marketing** toggle (`ad_storage`) activated |
| Privacy posture | stays in the analytics lane we already operate | opens an ads-cookie surface |
| Bidding | imported key event drives Max-Conversions fine | marginally tighter gclid attribution |

For a €400 proxy-conversion experiment the GA4-import path is strictly simpler and keeps the existing privacy architecture intact. The slightly fuzzier attribution + few-hour import lag are irrelevant at this scale.

### Mechanism

- New helper in `frontend/lib/analytics.ts`, e.g. `trackRegisterClick()`, that calls `window.gtag?.('event', 'register_click', { … })`.
  - Guarded: no-op unless `isApexBuild` and `typeof window !== 'undefined'` and `window.gtag` exists.
  - **Do not delay navigation.** GA4's transport uses `navigator.sendBeacon`, which survives the cross-domain unload to `app.thebetterdecision.com`. Fire on click, let the normal `<a>`/`<Link>` navigation proceed. No `event_callback` gymnastics.
  - **Do not self-gate on consent.** The existing `GoogleAnalytics.tsx` boots `gtag('consent','default', …all-denied…)` before `config`; Consent Mode redacts/modeled-pings the event automatically based on the stored Analytics choice. We just fire it.
- Wire it to every signup CTA. Single source of truth is `signupHref()` (`frontend/lib/links.ts`). Render sites today: `TopNav`, `Hero`, `SecondCta`, `VsPageLayout`, plus any `/features` `/compare` CTA. **Preferred:** introduce one shared `SignupLink` (or `SignupButton`) component that renders `<Link href={signupHref()} onClick={trackRegisterClick}>` and swap the call sites to it, so the event can never drift away from the CTA. Acceptable fallback if a shared component is disproportionate: attach `onClick={trackRegisterClick}` at each render site. Plan stage decides based on what the call sites actually look like.

### Event shape

```js
gtag('event', 'register_click', {
  // optional, useful for later ad-group attribution in GA4:
  cta_location: 'hero' | 'topnav' | 'second_cta' | 'vs_page' | 'features' | 'compare',
});
```

`event` name `register_click` is the contract the operator imports into Ads — **do not rename it without updating the Ads conversion import.**

### Out of scope (consciously)

- No native Google Ads tag, no `AW-` config, no CSP edits, no ads/doubleclick endpoints.
- No remarketing / Display audiences (would need the messy `google.<cctld>/ads/ga-audiences` pixels — not worth it).
- No in-app (authenticated host) tracking. The privacy policy's "no analytics on the signed-in app" stays true.
- Marketing consent toggle stays dormant.

### Testing

- Unit: `trackRegisterClick` no-ops when not apex / no `gtag`; calls `gtag('event','register_click', …)` when apex + gtag present.
- The apex build cannot run in the dev container (`next.config.ts` bind-mount); composition stays covered by `build-apex.test.ts` + CI, per prior sessions.
- Manual (operator, post-deploy): GA4 → Realtime, click a CTA, confirm `register_click` appears.

---

## Campaign blueprint (operator pastes this into Google Ads)

### Account / billing

- Finish the parked Google Ads account setup; ensure a payment method is attached so ads can serve (the **€400 credit applies first**; enter the promotional code if not already applied). Watch the credit's **expiry/spend-threshold conditions** — Google credits usually require spending a matching amount within ~60 days; pace accordingly.

### Settings

- **Campaign type:** Search. When prompted for a goal, choose **"Create a campaign without a goal's guidance"** (prevents Google from nudging you into Performance Max / Display).
- **Networks:** Search only. **Uncheck "Search Network partners". Uncheck "Display Network".** (Both leak budget.)
- **Locations:** target by **presence** ("People in or regularly in your targeted locations" — *not* "presence or interest"). Countries (high English proficiency, EU/EEA): **Netherlands, Ireland, Belgium, Luxembourg, Germany, Austria, Denmark, Sweden, Finland, Norway, Portugal.** (Start here; widen later if volume is thin.)
- **Language:** English.
- **Budget:** **€13/day** (≈ €390 over 30 days).
- **Bidding:** start **"Maximize clicks"** with a **max CPC limit of €0.60** for ~2 weeks to gather cheap click + `register_click` data. Once ~20–30 `register_click` conversions have accumulated, switch to **"Maximize conversions."**
- **Conversion goal (campaign level):** `register_click` only (set as the account's Primary conversion; see runbook).
- **Ad rotation:** Optimize.

### Ad groups, keywords, landing pages

Match type = **phrase match** unless noted. Start with the 4 groups below; the spreadsheet group is optional/later.

**AG1 — Cash-flow forecasting** → final URL `https://thebetterdecision.com/features`
```
"cash flow forecasting app"
"personal cash flow forecast"
"financial forecasting app"
"budget forecasting app"
"forecast bank balance"
"personal finance forecasting"
"money forecasting app"
"predict future balance"
```

**AG2 — PocketSmith alternative** → final URL `https://thebetterdecision.com/vs/pocketsmith`
```
"pocketsmith alternative"
"alternative to pocketsmith"
"apps like pocketsmith"
"pocketsmith competitor"
```

**AG3 — Monarch Money alternative** → final URL `https://thebetterdecision.com/vs/monarch`
```
"monarch money alternative"
"alternative to monarch money"
"apps like monarch money"
"monarch money competitor"
```

**AG4 — YNAB alternative** → final URL `https://thebetterdecision.com/vs/ynab`
```
"ynab alternative"
"alternative to ynab"
"apps like ynab"
"cheaper than ynab"
"ynab competitor"
```

**AG5 — Spreadsheet replacement** *(optional, add after first week)* → `https://thebetterdecision.com/vs/spreadsheets`
```
"budget spreadsheet alternative"
"replace budget spreadsheet"
"budgeting without spreadsheets"
```

### Negative keywords (campaign level)

```
excel
template
templates
course
courses
udemy
coursera
free download
job
jobs
salary calculator
loan
loans
crypto
stocks
trading
invoice
accounting software
for business
api
```

### Responsive Search Ads (RSA)

One RSA per ad group. Headlines ≤30 chars, descriptions ≤90 chars. Pin nothing; let Google rotate. Reuse the shared headlines across groups where natural.

**Shared headlines (usable in any group):** `See Your Future Balance` · `Free While in Beta` · `The Better Decision` · `Plan Your Cash Flow` · `Plan Before You Spend` · `Stop Guessing Your Finances`

**AG1 — Forecasting**
- Headlines: `Cash Flow Forecasting App` · `Forecast Your Money` · `Know Your Balance Ahead` · `Personal Finance Forecasting` · `Predict Your Cash Flow` · `Budget With Foresight` · + shared
- Descriptions:
  - `Forecast your balance weeks and months ahead. Plan with confidence, not hindsight.`
  - `Personal finance built around forecasting — not just tracking the past. Free in beta.`
  - `Import your bank data and see where your money is headed. Try it free.`
  - `Line-item clarity on every euro. Plan first, spend smart.`

**AG2 — PocketSmith alternative**
- Headlines: `PocketSmith Alternative` · `Apps Like PocketSmith` · `Switch From PocketSmith` · `Forecasting, Less Clutter` · `Modern Finance Forecasting` · + shared
- Descriptions:
  - `Looking beyond PocketSmith? Forecast your balance with a cleaner, modern tool.`
  - `Forecasting-first personal finance. Import, plan, and see what's ahead. Free in beta.`
  - `All the foresight, none of the clutter. Try The Better Decision free.`
  - `Plan your money months ahead with line-item clarity.`

**AG3 — Monarch Money alternative**
- Headlines: `Monarch Money Alternative` · `Apps Like Monarch Money` · `Switch From Monarch` · `Forecasting-First Finance` · `Beyond Budget Tracking` · + shared
- Descriptions:
  - `Beyond Monarch's tracking — forecast where your money is actually headed. Free in beta.`
  - `Personal finance that plans ahead, not just looks back. Try it free.`
  - `Import your accounts and forecast your balance with clarity.`
  - `A modern alternative built around foresight. Free while in beta.`

**AG4 — YNAB alternative**
- Headlines: `YNAB Alternative` · `Apps Like YNAB` · `Cheaper Than YNAB` · `Forecast, Don't Just Budget` · `Budgeting With Foresight` · + shared
- Descriptions:
  - `Love budgeting but want forecasting too? See your balance months ahead. Free in beta.`
  - `A YNAB alternative that forecasts the future, not just assigns the present.`
  - `Import, plan, and predict your cash flow. Free while in beta.`
  - `Budgeting plus real forecasting, in one tool. Try it free.`

> Honesty guard (per prior /vs live-claim lesson): keep competitor claims accurate to the live `/vs/*` page copy. Don't assert a competitor "lacks AI/forecasting" in an ad if the page doesn't stand behind it.

---

## Operator runbook (ordered — there is a hard dependency on deploy)

**Phase 0 — after the engineering PR ships to prod**
1. Open `https://thebetterdecision.com`, accept Analytics in the consent banner, click any **Get Started**. In **GA4 → Reports → Realtime**, confirm the **`register_click`** event fires.

**Phase 1 — link GA4 ↔ Google Ads**
2. **GA4 Admin → Product links → Google Ads links →** link the Ads account. Leave auto-tagging **on** (default) so GA4 captures `gclid`.
3. **GA4 Admin → Events → Key events →** mark **`register_click`** as a Key event. *(It only appears here after it has fired at least once — that's why Phase 0 comes first.)*

**Phase 2 — import the conversion into Ads**
4. **Google Ads → Goals → Conversions → + New conversion action → Import → Google Analytics 4 (web) →** select **`register_click` → Import**. Set it as a **Primary** conversion goal.

**Phase 3 — build & launch the campaign**
5. New **Search** campaign → "without a goal's guidance" → apply the **Settings**, **Ad groups/keywords**, **Negatives**, and **RSAs** above → Review → **Publish**.

**Phase 4 — monitor (light touch)**
6. After ~3–5 days: open the **Search terms report**, add junk queries as negatives, pause any keyword with spend and zero clicks/quality.
7. Cross-check **`register_click` count vs. actual new signups** in the app admin. A big gap (many clicks, few signups) means the **register page / onboarding is the leak** — a genuine product learning, not an ad problem.
8. After ~20–30 `register_click` conversions: switch bidding to **Maximize conversions**.

---

## Success metrics

- Primary: **`register_click` conversions** and **cost per `register_click`**, by ad group → tells us which positioning pulls.
- Secondary: clicks, CTR, avg CPC, impression share.
- Ground truth: **actual new signups in the admin** during the flight, cross-checked against `register_click`.
- Bar for "worth it": within the €400 credit, produce measurable `register_click` volume, a handful of real strangers signing up, and a clear read on the cheapest-to-acquire ad group to inform any future paid spend.

## Risks / honest caveats

- **`register_click` is a proxy** (intent to register, fired on the apex), not a completed account. The admin cross-check (step 7) is the corrective.
- **Thin EU-English volume** on competitor terms may limit delivery; Max-Clicks + the forecasting group hedge this.
- **Consent redaction:** EU users who reject Analytics produce modeled/under-counted conversions. Acceptable at this scale.
- **Spending to acquire un-monetized beta users** is deliberate — the return is feedback + funnel learning, not revenue. Keep it a one-credit experiment, not a habit, until there's a monetization path.
- **Credit expiry:** Google ad credits typically must be matched by spend within a window — confirm the terms so the credit isn't wasted.
```
