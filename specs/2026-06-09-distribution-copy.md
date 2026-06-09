# Distribution copy for launch (2026-06-09)

> Working doc, not part of any site build or PR. Hand-editable copy for the first
> distribution push behind the new /features, /compare, and /vs pages.
> No em-dashes (locked policy). Every claim below is honest and matches the
> comparison pages: CSV/OFX import (no bank linking), EU-hosted, bring-your-own or
> local AI, free while in beta.

---

## 1. Realistic expectations

Set these before spending a weekend on it:

- **Social and forum posts drive the first wave in days, not the comparison pages.** A good Show HN or a genuinely helpful Reddit thread can send real visitors within hours. That traffic is spiky and fades in a week.
- **The /compare and /vs pages compound over a quarter.** They are SEO and GEO assets. A brand-new domain has little authority, so Google ranking for "YNAB alternative" or "budget without a spreadsheet" takes weeks to months, and AI engines start citing the pages only once they have crawled and trust them. Publish them, link them internally, then be patient.
- **The domain is new.** Do not expect page-one rankings in week one. The honest comparison angle (we concede where competitors win) is what earns links and citations over time, which is what actually moves ranking.
- **One Show HN shot.** You realistically get one good front-page attempt. Post only when the app is solid and the landing pages are live.

A reasonable mental model: forums for the first 200 visitors, comparison pages for the next 2000 over the following months.

---

## 2. Show HN

Post only when the app is stable, signup works end to end, and /features + /compare are live. One shot.

**Title:**

```
Show HN: The Better Decision, a privacy-first budgeting and cash-flow forecasting app
```

**Body draft:**

```
I built The Better Decision because I wanted to see what is coming, not just
categorize what already happened. It is a budgeting and cash-flow forecasting
app: recurring income and bills roll forward into a projected end-of-month
balance, with what-if scenarios on top of normal category budgets.

Two things make it different from the usual budgeting app:

1. No bank linking. It imports CSV or OFX files, with a preview before anything
   is written. Nothing connects to your bank automatically. Some people hate
   this, some people specifically want it.

2. The data plane is self-hosted in the EU. MySQL and Redis run on a dedicated
   droplet in a private VPC, processed under EU law, and the app is a 12-factor
   stateless backend (FastAPI) behind a Next.js frontend. Your data is never
   sold and never used to train AI.

AI is opt-in and on your terms: bring your own OpenAI or Anthropic key, or run
it entirely locally with Ollama. There are hard and soft spend caps and a full
audit trail of every call. AI suggests a category, refines a forecast, or
rebalances a budget, and you accept or reject every suggestion before anything
is saved. There is no hosted-by-us AI yet, and connecting it to your own
assistant over MCP is on the roadmap, not shipped.

It is free while in beta. I would genuinely value critique on the forecasting
model and the import flow. Honest comparison with YNAB, PocketSmith, Monarch,
and spreadsheets is on the site, including where each of them beats us.

Live: https://thebetterdecision.com
```

Notes:
- The HN-resonant angle is the self-hosted EU data plane and the bring-your-own / local AI with spend caps. Lead with that, not with generic budgeting.
- Be present in the thread for the first few hours to answer architecture questions.
- Do not oversell. The "where they win" honesty plays well with this audience.

---

## 3. Reddit "helpful founder" comment template

Use only where you genuinely help first. Lead with a real answer to the person's
actual problem, then one soft, optional line. Never drop a bare link.

**Template:**

```
For what you are describing, the thing that helped me most was separating "what
already happened" from "what is coming." Most budgeting tools are great at
categorizing the past but leave you guessing about next month. A few concrete
things that worked:

- List your recurring income and bills and roll them forward, so you see a
  projected balance for the end of the month instead of just today's number.
- Reconcile imported transactions against that plan weekly, not daily, so you
  are not living in the app.
- Keep one or two what-if scenarios (a big expense, a slow income month) so a
  surprise is not a surprise.

You can do all of this in a spreadsheet, and honestly for a lot of people a
spreadsheet is the right answer. If you want it automated without linking your
bank, I have been building a small app around exactly this (CSV/OFX import,
forecasting, EU-hosted, free while in beta). Happy to share if useful, but the
workflow above matters more than the tool.
```

**Rules note (read before posting):**
- r/personalfinance and r/Budget aggressively auto-remove anything that looks like promotion, even a single link from a low-karma account. Participate genuinely for a while first; link rarely, if ever.
- r/eupersonalfinance and smaller niche subs are more tolerant of a founder who is clearly helping, especially given the EU data-residency angle.
- Build comment karma and history before you ever mention the product. A new account that only posts links gets shadow-removed.
- Prefer answering questions where forecasting or no-bank-linking is the actual ask. Do not force the mention.

---

## 4. Subreddit shortlist

Tolerance notes per sub. "Tolerant" still means help-first, link-rarely.

| Subreddit | Fit | Promotion tolerance |
|---|---|---|
| r/eupersonalfinance | High. EU data residency and no-bank-linking resonate directly. | Moderate. Founder participation accepted if genuinely helpful. |
| r/ynab | High intent. People here are actively comparing budgeting methods. | Low to moderate. Mention only as a thoughtful alternative, concede YNAB's strengths, never bash. |
| r/plaintextaccounting | Niche, technical, privacy-minded. Import-from-files and self-hosting resonate. | Moderate. This crowd values the no-bank-linking and EU-hosting story. |
| r/selfhosted | Tangential but the EU self-hosted data plane and local Ollama AI fit the ethos. | Moderate, IF framed around the architecture, not the budgeting features. |
| r/personalfinance | Huge reach, lowest tolerance. | Very low. Help only. Treat any link as removal-bait. |
| r/Budget | Smaller, on-topic. | Low. Auto-moderation removes promotion; participate first. |
| r/PFtools or similar tool-discussion subs | On-topic for "what app do you use." | Moderate. A genuine answer in a tool-recommendation thread is acceptable. |

General rule across all of them: be a person who helps, not an account that
links. The comparison pages do the selling once people search; the forums just
need to make them aware you exist.
