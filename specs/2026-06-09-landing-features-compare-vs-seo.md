# Landing expansion: /features, /compare, and /vs comparison pages

**Date:** 2026-06-09
**Status:** Spec, pending user review
**Author:** brainstormed dev + architect, reconciled

## Goal

Attract organic discovery (Google + AI engines: ChatGPT, Perplexity, Claude,
Google AI Overviews) for "The Better Decision" (tbd), a brand-new, near-zero
authority personal-finance web app, with no ad budget. Expand the public
marketing surface with a feature hub, a comparison matrix, and a small set of
honest competitor comparison pages, plus a homepage content pass that surfaces
more real features and links the new pages.

The lead angle everywhere is **forecasting first, EU-privacy as the immediate
second beat** ("budgeting and cash-flow forecasting together in one calm app,
EU-hosted, for normal people"), with one honest exception noted below
(`/vs/pocketsmith`).

This is expansion, not a rebuild. The existing landing page
(Hero -> AnswerLead -> FeatureTiles -> ScreenshotShowcase -> HowItWorks -> FAQ)
and the SEO/GEO plumbing stay; we add to them.

## Non-goals

- No blog/content hub, no `/pricing`, no `/help` (footer already points Help at
  `/docs`, a real 17-section manual).
- No AI claims about **unbuilt** features. Shipped AI is marketed honestly (see
  "AI capabilities" below); backlog AI (MCP/assistant, native hosted provider,
  advanced generation params) appears ONLY in a clearly-labeled "On the roadmap"
  block, never implied as available, and never in the comparison matrix or
  `featureList` JSON-LD.
- No price story or permanent-price claims (payments are hidden per PR #378;
  product is "free while in beta").
- No `Review` / `AggregateRating` / `Offer` structured data (no real reviews;
  it would be both a Google policy violation and dishonest).
- No live bank aggregation claim (the app imports CSV/OFX only).
- The Reddit/Show HN distribution work is off-codebase founder activity; only
  the *copy* for it is a deliverable (a side doc), not part of the site build.

## Routes (6)

| Route | Role | Publish |
|---|---|---|
| `/features` | Feature hub: real shipped capabilities (incl. a shipped-AI group and a labeled "On the roadmap" block, see "AI capabilities"), grouped by the 4-beat arc, with a condensed "how we compare" teaser linking to `/compare`. | now |
| `/compare` | Canonical home of the full multi-competitor matrix + spoke hub linking to each `/vs` page. | now |
| `/vs/spreadsheets` | Deep, distinct comparison vs DIY budgeting spreadsheets. | now |
| `/vs/ynab` | Deep, distinct comparison vs YNAB. | now |
| `/vs/pocketsmith` | Deep, distinct comparison vs PocketSmith. | **staggered** |
| `/vs/monarch` | Deep, distinct comparison vs Monarch. | **staggered** |

All six are **static `page.tsx` routes** (`app/features/page.tsx`,
`app/compare/page.tsx`, `app/vs/<x>/page.tsx`) — not a dynamic `[slug]` route.
The four `/vs` pages must be genuinely distinct (own intro, own table slice, own
"where they win" block, own FAQ); a dynamic template fights that requirement and
adds a silent-missing-page failure mode under static export. This mirrors the
existing `app/docs/page.tsx` + `app/docs/plans/page.tsx` pattern (each exports
its own `metadata` and JSON-LD).

### Staggered publication (PocketSmith + Monarch)

Four templated comparison pages published at once on a zero-authority domain
read as a doorway cluster to Google and can suppress all of them. So:

- **PR 3 ships all four `/vs` page files** (full code review covers them).
- `spreadsheets` + `ynab`: `robots: { index: true, follow: true }`, listed in
  `sitemap.ts`, the apex sitemap heredoc, `llms.txt`, and linked from `/compare`
  and `/features`.
- `pocketsmith` + `monarch`: `robots: { index: false, follow: false }`,
  **omitted** from both sitemaps and `llms.txt`, and **not linked** from
  `/compare` or `/features` (their *matrix columns* still show, but the
  "see full comparison ->" deep-link is withheld).
- A small follow-up **publish PR** (~2-4 weeks later, once the first two are
  indexed) flips both to `index: true`, adds them to both sitemaps + `llms.txt`,
  and wires the `/compare` + `/features` deep-links.

## Positioning per page (honest, distinct)

Cross-cutting rule: **answer-first opening**, a **comparison table with text in
cells** (not checkmarks alone), and an honest **"where they win" concession**.
The concession is the strategy, not a tax: balanced trade-off pages are what AI
engines cite from a low-authority source, and what makes each page
non-templatable. Concede **bank sync** explicitly on YNAB/PocketSmith/Monarch
(the app is CSV/OFX import only), framed as a deliberate choice
("no account linking required").

- **`/vs/spreadsheets`** (strongest page). Contrast: recurring bills/income roll
  forward automatically into a projected end-of-month balance and imports
  reconcile against the plan, with no formulas to maintain. *They win:* total
  flexibility, free forever, no signup, no new tool to learn.
- **`/vs/ynab`** (highest commercial intent). Contrast: YNAB is zero-based
  envelope budgeting for *this month*; we add the forward view it omits
  (projected balance across accounts, what-if scenarios), EU-hosted, lighter
  touch. *They win:* live bank sync, a proven method with a long-running
  community, deep educational content, mature mobile app. EU-privacy wedge is
  legitimate (YNAB is US).
- **`/vs/pocketsmith`** (weakest /vs; write carefully). Forecasting is
  *their* headline feature, so do **not** claim to out-forecast them, and do
  **not** lead with EU-privacy (PocketSmith is NZ, which has EU adequacy).
  Contrast downshifts to: same forward view in a simpler, calmer,
  household-shared app without spreadsheet-grade complexity; EU-residency stated
  as a locality preference, not a safety claim. *They win:* far deeper
  forecasting (long-range projections, multiple scenarios, multi-currency), more
  bank-feed coverage, maturity.
- **`/vs/monarch`**. Contrast: Monarch is polished US tracking + net-worth on
  bank aggregation; we lead with planning (projected balance, what-if), EU-hosted,
  no aggregation required. *They win:* comprehensive live bank/investment sync,
  net-worth tracking, polished UX, established review base. EU-privacy wedge is
  legitimate (US).

## AI capabilities (shipped + roadmap)

A genuinely differentiating, on-thesis (forecasting + privacy) story, and one no
named competitor matches honestly. Lives primarily as a feature group on
`/features`, surfaced lightly on the homepage strip, and as one matrix dimension.

**Shipped — market as real** (all verified in code; all accept/reject before any
write, all feature-gated and provider-required):

- Auto-categorize transactions (`/api/v1/ai/categorize`).
- AI forecast refinement: seasonal patterns + anomaly flags, review-then-apply
  per category (`/api/v1/ai/forecast/refine`, #420).
- Budget rebalance suggestions, per-row accept/skip (`/api/v1/ai/budget/rebalance`).
- **Bring-your-own AI: OpenAI, Anthropic, or local Ollama** — the privacy-first
  wedge: use your own key or run it entirely locally.
- **Spend caps** (soft alert + hard refuse), full **audit trail** of every call,
  encrypted-at-rest credentials.

The headline for this group is the BYO/local + caps + audit angle, not "we have
AI." Frame it as control and privacy, consistent with the no-"AI-powered"-hype
brand voice.

**Roadmap — clearly labeled "On the roadmap", never as available:**

- MCP server + AI chat assistant: connect your finances to your own AI assistant,
  with every change confirmed before it runs. (This is the integrate-with-your-
  own-AI-harness draw; it is backlog — spec `ai-assistant-mcp-chat-backlog.md`.)
- Native TBD-hosted AI provider (opt-in, consent-gated).
- Advanced generation parameters.

The roadmap block must be visually and semantically distinct from the shipped
group (its own heading, "on the roadmap" language), carry NO checkmarks/"yes"
claims, and is excluded from `featureList` JSON-LD and the comparison matrix.

**Matrix dimension** is `privacyFirstAi` framed as *bring-your-own / local,
privacy-first AI* — the cell where tbd is uniquely "yes". It is NOT "has any AI":
Monarch has shipped its own AI assistant, so competitor AI cells must be
fact-checked when authoring `lib/comparison.ts`, not asserted from memory. The
honest contrast is BYO/local/caps/audit, not presence of AI.

## Architecture

### Single source of truth for comparison facts

New typed, pure data module **`frontend/lib/comparison.ts`** (precedent:
`faqData.ts`, `howItWorksData.ts`, `lib/site.ts`). The `/compare` matrix renders
the full object; each `/vs/<x>` page renders the 2-column `tbd`-vs-`<x>` slice of
the **same** object plus its concession list. Hand-authoring the facts twice
would let them drift, and drift on a comparison table is a credibility/honesty
problem.

Sketch:

```ts
export type Competitor =
  | "tbd" | "ynab" | "pocketsmith" | "monarch" | "spreadsheets";
export type Dimension =
  | "forecasting" | "budgeting" | "euResidency"
  | "householdSharing" | "bankSync" | "privacyFirstAi" | "price";

export interface Cell {
  supported: "yes" | "no" | "partial";
  value: string; // short factual phrase shown IN the cell (AEO-extractable)
}
export const comparisonMatrix: Record<Dimension, Record<Competitor, Cell>>;
export const competitorMeta: Record<Competitor, {
  name: string;
  whereTheyWin: string[];
}>;
```

- `tbd` price cell is `"Free while in beta"` (never a hard price or "Free").
- A unit test asserts the matrix is dense (every dimension x competitor filled).

### New shared components

- **`frontend/components/landing/ComparisonTable.tsx`** — accessible `<table>`
  (`<th scope>`), token-only colors (CI-checked by `check-design-tokens.sh`),
  a check/dash glyph as inline `aria-hidden` SVG with an sr-only text label per
  cell (never color/emoji alone; WCAG 2.2 AA). Consumed by `/compare` (full) and
  each `/vs` page (2-column slice).
- **`frontend/components/landing/VsPageLayout.tsx`** — wraps the shared skeleton
  (TopNav -> H1 -> answer intro -> table slice -> "where they win" -> forecasting
  closer -> CTA -> LandingFooter) so the four pages stay structurally consistent
  and only data/copy differ.

### Reuse

`SecondCta`, the `Faq` `<details>`/`<summary>` markup pattern (per-page data, not
the shared `faqEntries`), `TopNav`, `LandingFooter`, the section-wrapper idiom
(`mx-auto max-w-* px-6 py-20`), card idiom (`rounded-xl border border-border
bg-surface p-6`), and `lib/styles.ts` (`btnPrimary`, `card`). **All CTAs route
through `lib/links.ts` (`signupHref`/`signinHref`)** — never a bare `/register`,
which would break across the host boundary on apex.

All new pages are **server components** (no `"use client"`) so JSON-LD and copy
land in the initial HTML for crawlers.

## SEO / GEO plumbing (the easy-to-forget half)

Each route must appear correctly on **both hosts**. The app SSR host
(`app.thebetterdecision.com`) is driven by `app/sitemap.ts` + `app/robots.ts`;
the apex static host (`thebetterdecision.com`) is driven by **hand-written bash
heredocs** in `scripts/build-apex.sh` (because `sitemap.ts`/`robots.ts` are
staged out of the apex export). These are two separate sources that drift — every
new route touches both.

**`frontend/scripts/build-apex.sh`:**
- `ALLOWED_ROUTE_DIRS` += `"features"`, `"compare"`, `"vs"` (the single `vs`
  entry ships all four child pages; the stager matches top-level dir basenames,
  not recursively).
- `ALLOWED_OUTPUT_GLOBS` += `"features"`, `"compare"`, `"vs"` — **mandatory** or
  the post-build top-level guard hard-fails on the new output dirs.
- apex `sitemap.xml` heredoc += `<url>` lines for `/features/`, `/compare/`,
  `/vs/spreadsheets/`, `/vs/ynab/` (trailing slashes, to match
  `trailingSlash: true`). PocketSmith/Monarch added in the publish PR.
- robots heredoc: no change (it is `Allow: /` catch-all).

**App SSR host:**
- `app/sitemap.ts` += entries for `/features`, `/compare`, `/vs/spreadsheets`,
  `/vs/ynab` (no trailing slash on this host), `priority` ~0.7,
  `changeFrequency: "monthly"`.
- `app/robots.ts` `allow` += the same paths (explicit-allowlist convention).

**Per-page metadata** (mirror `app/docs/page.tsx`):
- `alternates.canonical: apexCanonical("/vs/ynab")` — every shared page
  canonicalizes to apex so the app-subdomain copy doesn't split ranking signal.
- `...pageSocialMeta({ title, description, path: apexCanonical(...) })` — reuses
  the static `/og.png`. **Do not** add per-page dynamic `opengraph-image.tsx`
  routes; they are not exported under `output: 'export'` and 404 on apex.
- `robots: { index: true, follow: true }` — **required**; the root layout
  defaults to `{ index: false, follow: false }`. (Deferred pages use
  `index: false` until the publish PR.)

**`frontend/public/llms.txt`** += `/features/`, `/compare/`, `/vs/spreadsheets/`,
`/vs/ynab/` with one-line honest descriptions (PocketSmith/Monarch in publish PR).

### JSON-LD (reuse the linked-`@id` pattern from `app/page.tsx`)

Each inline JSON-LD `<script>` copies the existing nonce + escaping pattern
exactly: async server component, `await readNonce()`, conditional spread of the
nonce prop (empty on apex), and `.replace(/<\/script>/gi, ...)` escaping.

| Page | Schema |
|---|---|
| `/features` | `SoftwareApplication` with `featureList` (honest shipped features) + `BreadcrumbList`, referencing the existing `#organization` `@id`. |
| `/compare` | `BreadcrumbList` + `FAQPage` (comparison-level Q&As). The matrix is plain accessible HTML; there is no consumed "comparison" schema type. |
| `/vs/<x>` | `FAQPage` (per-page distinct Q&As) + `BreadcrumbList`. Optional `WebPage` `about`/`mentions` tying tbd `@id` + the competitor as a named entity. |

Also add `featureList` to the homepage `SoftwareApplication` block. `featureList`
(home and `/features`) includes only **shipped** features — including the shipped
AI ones — and never the roadmap MCP/assistant/native items.

### GEO/AEO rules baked into copy

- First 1-2 sentences after each H1 directly answer the implied question in a
  self-contained, quotable form (states what tbd is, who it's for, and concedes
  the gap).
- Comparison cells carry short factual phrases, not bare checkmarks.
- FAQ headings are literal questions ("Is The Better Decision a good YNAB
  alternative?", "Does it sync with my bank?").
- `/vs` `<title>`/H1/H2 carry the "alternative" phrasing (e.g.
  "A YNAB alternative built around forecasting") so the clean `/vs/<x>` URL still
  captures "X alternative" intent.
- Each H2 section reads standalone (LLMs chunk by heading; avoid "as above").

### Internal linking (spend scarce equity deliberately)

- Homepage links to `/features` and `/compare` only (not all four `/vs` in nav —
  that amplifies the doorway signal). `/compare` is the spoke hub to each `/vs`.
- `/compare` <-> each published `/vs/<x>` (bidirectional).
- Each `/vs/<x>` links to `/features` and to the most relevant `/docs` page
  (e.g. `/vs/spreadsheets` -> `/docs/plans`).
- `/features` links into `/docs` at feature level (forecasting -> `/docs/plans`).
- Every `/vs` and `/features`/`/compare` page CTAs to `/register`.

## Homepage content pass

- Sharpen `Hero` subcopy to lead forecasting, EU-privacy as the second beat.
- Add an **"Everything in the app"** feature strip after `ScreenshotShowcase`
  surfacing more real features (recurring, reports, budgets, import, and the
  shipped BYO/local AI), linking to `/features`. (This is the "add more features"
  ask, on the highest-traffic page.) No roadmap/unbuilt items on the homepage.
- Add a quiet differentiator line linking `/compare`.
- Leave AnswerLead, HowItWorks, FAQ intact. Add `featureList` to the homepage
  `SoftwareApplication` JSON-LD.

## Honesty guardrails (restate; enforce in review)

- No em-dashes in customer copy.
- No invented testimonials; verify `Testimonials.tsx` stays unrendered/empty (a
  comparison push is exactly when someone is tempted to wire it back in).
- Shipped AI marketed honestly; unbuilt AI (MCP/assistant, native provider) only
  in the labeled roadmap block, never as available, never in matrix/`featureList`.
- No permanent-price/"cheaper than X" claims.
- Concede bank sync on YNAB/PocketSmith/Monarch.
- vs PocketSmith: do not overclaim forecasting or EU-privacy.
- No `Review`/`AggregateRating`/`Offer` schema.

## Testing

Extend:
- `tests/sitemap-includes-docs.test.ts` — assert the new published paths present.
- `tests/seo-public-routes-indexable.test.tsx` — add the published pages to
  `indexableMetadatas` (guards the `robots: { index: true }` export); assert the
  deferred pages are `index: false` until the publish PR.
- `tests/build-apex.test.ts` — assert `ALLOWED_ROUTE_DIRS` and
  `ALLOWED_OUTPUT_GLOBS` contain `features`/`compare`/`vs`, and the apex sitemap
  heredoc lists the published `/vs/*` paths.

New:
- Comparison-data integrity: matrix fully populated, no empty cells, `tbd` price
  is the beta string.
- `ComparisonTable` a11y: `<th scope>` present, per-cell sr-only labels, no raw
  palette colors.
- Per-`/vs` `FAQPage` JSON-LD validity (clone `landing-jsonld-faqpage.test.tsx`):
  valid `mainEntity` questions and `</script>` escaping present.
- Content lint: no em-dash in the comparison strings.
- AI honesty: assert the `/features` roadmap block carries no "yes"/checkmark
  claims; assert `featureList` JSON-LD (home + `/features`) contains none of the
  roadmap items (MCP/assistant/native/generation-params).

The CI `build:apex` run is the backstop: a route added to `ALLOWED_ROUTE_DIRS`
without `ALLOWED_OUTPUT_GLOBS` fails it. Get it green before merge.

## PR slicing

Branch + PR each (never push to main); conventional-commit PR titles (squash
subject is the release/deploy gate); no test-plan section; no AI attribution;
test the feature branch on DO before merge.

1. **`feat(landing): comparison data model + ComparisonTable primitive`** —
   `lib/comparison.ts`, `ComparisonTable.tsx`, `VsPageLayout.tsx` + unit tests.
   Pure, no routes; honesty/claims reviewed in isolation.
2. **`feat(landing): /features and /compare pages`** — both routes + the full
   two-host plumbing (allowlist, output globs, both sitemaps, robots, llms.txt,
   canonical/social/robots metadata, JSON-LD) + indexable/apex tests. Proves the
   pipeline on two routes before multiplying by four.
3. **`feat(landing): /vs comparison pages`** — four `/vs/<x>` pages
   (spreadsheets + ynab indexed & linked; pocketsmith + monarch noindex &
   unlinked), per-page `FAQPage` JSON-LD + tests.
4. **`feat(landing): homepage forecasting-first content pass`** — Hero copy,
   "Everything in the app" strip, `/compare` link, homepage `featureList`.
5. **Later — `feat(landing): publish PocketSmith and Monarch comparisons`** —
   flip both to `index: true`, add to both sitemaps + `llms.txt` + `/compare`
   and `/features` deep-links.

## Side deliverable (not a site PR)

A markdown doc with **Show HN** (honest title + body draft) and a reusable
**"helpful founder" Reddit comment template**, plus a short note on subreddit
self-promo rules (r/personalfinance, r/Budget auto-remove promo;
r/eupersonalfinance and smaller subs more tolerant) and the realistic timescale
(social/HN drives the first wave in days; the content asset compounds over a
quarter). Lives in `specs/` (the repo `docs/` folder is git-ignored), kept out
of the `frontend/` build entirely.
