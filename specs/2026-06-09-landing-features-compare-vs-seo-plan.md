# Landing expansion: /features, /compare, /vs pages — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a feature hub, a comparison matrix, and four honest competitor comparison pages to the public marketing surface so the app gets discovered in Google and AI engines, while surfacing the shipped AI story and keeping every claim truthful.

**Architecture:** Six new static Next.js App Router pages (`/features`, `/compare`, `/vs/{spreadsheets,ynab,pocketsmith,monarch}`) built on two new shared primitives (`ComparisonTable`, `VsPageLayout`) fed by one typed source-of-truth data module (`lib/comparison.ts`). All pages render server-side (no `"use client"`), canonicalize to the apex host, and ship on both build targets. PocketSmith and Monarch ship as code but stay `noindex`/unlinked until a follow-up publish PR. Plumbing edits touch the two separate SEO sources (App SSR `sitemap.ts`/`robots.ts` and the apex bash heredocs).

**Tech Stack:** Next.js 16 App Router, React 19, TypeScript, Tailwind (design tokens only), Vitest + Testing Library. Spec: `specs/2026-06-09-landing-features-compare-vs-seo.md`.

**Conventions (locked):** branch + PR per slice, never push to main; conventional-commit PR titles (squash subject is the release/deploy gate); no test-plan section in PR bodies; no AI attribution in commits/PRs; no em-dashes in customer copy; design tokens only (CI: `frontend/scripts/check-design-tokens.sh`). Run frontend tests as the FULL suite (`npm test`), not single-file, per `reference_frontend_full_suite_verification`.

---

## File Structure

**New files:**
- `frontend/lib/comparison.ts` — typed matrix + competitor meta (single source of truth).
- `frontend/components/landing/ComparisonTable.tsx` — accessible comparison table (full or sliced).
- `frontend/components/landing/VsPageLayout.tsx` — shared `/vs` page skeleton + JSON-LD.
- `frontend/app/features/page.tsx` — feature hub + roadmap block + condensed compare teaser.
- `frontend/app/compare/page.tsx` — canonical full matrix + spoke hub.
- `frontend/app/vs/spreadsheets/page.tsx`, `frontend/app/vs/ynab/page.tsx`, `frontend/app/vs/pocketsmith/page.tsx`, `frontend/app/vs/monarch/page.tsx`.
- Tests: `frontend/tests/comparison-data.test.ts`, `frontend/tests/comparison-table.test.tsx`, `frontend/tests/vs-page-jsonld.test.tsx`, `frontend/tests/features-no-roadmap-in-featurelist.test.tsx`.

**Modified files:**
- `frontend/app/sitemap.ts`, `frontend/app/robots.ts`, `frontend/public/llms.txt`.
- `frontend/scripts/build-apex.sh` (two arrays + sitemap heredoc).
- `frontend/app/page.tsx` (Hero copy already in `Hero.tsx`; add `featureList`; add "Everything in the app" strip + `/compare` link).
- `frontend/components/landing/Hero.tsx` (forecasting-first subcopy).
- `frontend/tests/seo-public-routes-indexable.test.tsx` (add new routes).
- `frontend/tests/build-apex.test.ts` (assert new allowlist/heredoc entries).

**Deliverable doc (not in build):** `specs/2026-06-09-distribution-copy.md`.

---

# PR 1 — Comparison data model + primitives

Branch: `feat/comparison-primitives`. No routes, no SEO. Pure, isolated; this is where the honesty of every claim is reviewed.

## Task 1: Comparison data module (`lib/comparison.ts`)

**Files:**
- Create: `frontend/lib/comparison.ts`
- Test: `frontend/tests/comparison-data.test.ts`

- [ ] **Step 1: Write the failing test**

```ts
// frontend/tests/comparison-data.test.ts
import { describe, it, expect } from "vitest";
import {
  competitorOrder,
  dimensionOrder,
  comparisonMatrix,
  competitorMeta,
} from "@/lib/comparison";

describe("comparison data", () => {
  it("matrix is dense: every dimension x competitor has a cell", () => {
    for (const dim of dimensionOrder) {
      for (const comp of competitorOrder) {
        const cell = comparisonMatrix[dim]?.[comp];
        expect(cell, `${dim}.${comp}`).toBeDefined();
        expect(typeof cell.value).toBe("string");
        expect(cell.value.length).toBeGreaterThan(0);
        expect(["yes", "no", "partial"]).toContain(cell.supported);
      }
    }
  });

  it("tbd price is the beta string, never a hard price or 'Free'", () => {
    expect(comparisonMatrix.price.tbd.value).toBe("Free while in beta");
  });

  it("every competitor has a name and at least one honest 'where they win' point", () => {
    for (const comp of competitorOrder) {
      expect(competitorMeta[comp].name.length).toBeGreaterThan(0);
      if (comp !== "tbd") {
        expect(competitorMeta[comp].whereTheyWin.length).toBeGreaterThan(0);
      }
    }
  });

  it("contains no em-dashes in any customer-facing string", () => {
    const strings: string[] = [];
    for (const dim of dimensionOrder)
      for (const comp of competitorOrder)
        strings.push(comparisonMatrix[dim][comp].value);
    for (const comp of competitorOrder) {
      strings.push(competitorMeta[comp].name);
      strings.push(...competitorMeta[comp].whereTheyWin);
    }
    for (const s of strings) expect(s).not.toContain("—");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run tests/comparison-data.test.ts`
Expected: FAIL — cannot resolve `@/lib/comparison`.

- [ ] **Step 3: Write the data module**

```ts
// frontend/lib/comparison.ts
// Single source of truth for every comparison fact shown on /compare and the
// /vs/* pages. The matrix and each /vs table render slices of THIS object so
// the facts cannot drift. No em-dashes (locked policy). Cells state a short
// factual phrase (AEO-extractable), never a bare checkmark.
//
// HONESTY: competitor cells must be verified against the live competitor sites
// before publish, especially `privacyFirstAi` (Monarch ships its own AI
// assistant) and `price` (kept generic, no dollar amounts, no permanent claim).

export type Competitor =
  | "tbd"
  | "ynab"
  | "pocketsmith"
  | "monarch"
  | "spreadsheets";

export type Dimension =
  | "forecasting"
  | "budgeting"
  | "euResidency"
  | "householdSharing"
  | "bankSync"
  | "privacyFirstAi"
  | "price";

export interface Cell {
  supported: "yes" | "no" | "partial";
  value: string;
}

// Render order (tbd always first).
export const competitorOrder: ReadonlyArray<Competitor> = [
  "tbd",
  "spreadsheets",
  "ynab",
  "pocketsmith",
  "monarch",
];

export const dimensionOrder: ReadonlyArray<Dimension> = [
  "forecasting",
  "budgeting",
  "bankSync",
  "householdSharing",
  "euResidency",
  "privacyFirstAi",
  "price",
];

export const dimensionLabels: Record<Dimension, string> = {
  forecasting: "Cash-flow forecasting",
  budgeting: "Budgeting",
  bankSync: "Live bank sync",
  householdSharing: "Household sharing",
  euResidency: "EU data residency",
  privacyFirstAi: "Bring-your-own / local AI",
  price: "Price",
};

export const competitorMeta: Record<
  Competitor,
  { name: string; whereTheyWin: string[] }
> = {
  tbd: { name: "The Better Decision", whereTheyWin: [] },
  spreadsheets: {
    name: "Spreadsheets",
    whereTheyWin: [
      "Total flexibility: anything you can express in a formula.",
      "Free forever, no account, no new tool to learn.",
      "You own the file outright and can take it anywhere.",
    ],
  },
  ynab: {
    name: "YNAB",
    whereTheyWin: [
      "Live bank sync that imports transactions automatically.",
      "A proven zero-based method with a long-running community.",
      "Deep educational content and a mature mobile app.",
    ],
  },
  pocketsmith: {
    name: "PocketSmith",
    whereTheyWin: [
      "Deeper forecasting: long-range projections and multiple scenarios.",
      "Wider bank-feed coverage and multi-currency support.",
      "A mature product with years of refinement.",
    ],
  },
  monarch: {
    name: "Monarch",
    whereTheyWin: [
      "Comprehensive live bank and investment sync for net-worth tracking.",
      "A very polished, well-reviewed interface.",
      "An established user base and ecosystem.",
    ],
  },
};

export const comparisonMatrix: Record<Dimension, Record<Competitor, Cell>> = {
  forecasting: {
    tbd: { supported: "yes", value: "Projected balance plus what-if scenarios" },
    spreadsheets: { supported: "partial", value: "Only what you build by hand" },
    ynab: { supported: "no", value: "Envelope budgeting, no forward projection" },
    pocketsmith: { supported: "yes", value: "Long-range cash-flow projections" },
    monarch: { supported: "partial", value: "Basic cash-flow projection" },
  },
  budgeting: {
    tbd: { supported: "yes", value: "Category budgets and forecast plans" },
    spreadsheets: { supported: "partial", value: "Whatever you build yourself" },
    ynab: { supported: "yes", value: "Zero-based envelope method" },
    pocketsmith: { supported: "partial", value: "Budgeting is the secondary feature" },
    monarch: { supported: "yes", value: "Category budgets" },
  },
  bankSync: {
    tbd: { supported: "no", value: "CSV and OFX import, no account linking" },
    spreadsheets: { supported: "no", value: "Manual entry" },
    ynab: { supported: "yes", value: "Live bank sync" },
    pocketsmith: { supported: "yes", value: "Live bank feeds" },
    monarch: { supported: "yes", value: "Live bank and investment sync" },
  },
  householdSharing: {
    tbd: { supported: "yes", value: "Shared organization with roles" },
    spreadsheets: { supported: "partial", value: "Share the file and hope" },
    ynab: { supported: "yes", value: "Shared accounts" },
    pocketsmith: { supported: "yes", value: "Household accounts on paid tiers" },
    monarch: { supported: "yes", value: "Shared households" },
  },
  euResidency: {
    tbd: { supported: "yes", value: "EU-hosted, processed under EU law" },
    spreadsheets: { supported: "partial", value: "Wherever you keep the file" },
    ynab: { supported: "no", value: "US-based" },
    pocketsmith: { supported: "partial", value: "New Zealand, with EU adequacy" },
    monarch: { supported: "no", value: "US-based" },
  },
  privacyFirstAi: {
    tbd: { supported: "yes", value: "Your own key or local Ollama, with spend caps" },
    spreadsheets: { supported: "no", value: "None built in" },
    ynab: { supported: "no", value: "No AI features" },
    pocketsmith: { supported: "no", value: "No AI features" },
    monarch: { supported: "partial", value: "Built-in assistant, no bring-your-own or local" },
  },
  price: {
    tbd: { supported: "yes", value: "Free while in beta" },
    spreadsheets: { supported: "yes", value: "Free" },
    ynab: { supported: "no", value: "Paid subscription" },
    pocketsmith: { supported: "partial", value: "Free tier plus paid plans" },
    monarch: { supported: "no", value: "Paid subscription" },
  },
};
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run tests/comparison-data.test.ts`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/lib/comparison.ts frontend/tests/comparison-data.test.ts
git commit -m "feat(landing): comparison data model"
```

## Task 2: ComparisonTable primitive

**Files:**
- Create: `frontend/components/landing/ComparisonTable.tsx`
- Test: `frontend/tests/comparison-table.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/tests/comparison-table.test.tsx
import { describe, it, expect } from "vitest";
import { render, within } from "@testing-library/react";
import ComparisonTable from "@/components/landing/ComparisonTable";
import { dimensionOrder } from "@/lib/comparison";

describe("ComparisonTable", () => {
  it("renders a column per requested competitor with scoped headers", () => {
    const { getByRole } = render(
      <ComparisonTable competitors={["tbd", "ynab"]} />,
    );
    const table = getByRole("table");
    const colHeaders = within(table)
      .getAllByRole("columnheader")
      .map((th) => th.getAttribute("scope"));
    // first header is the dimension column, then one per competitor
    expect(colHeaders).toEqual(["col", "col", "col"]);
    expect(within(table).getByText("The Better Decision")).toBeTruthy();
    expect(within(table).getByText("YNAB")).toBeTruthy();
  });

  it("renders a row per dimension with a row header", () => {
    const { getByRole } = render(
      <ComparisonTable competitors={["tbd", "ynab"]} />,
    );
    const rowHeaders = within(getByRole("table")).getAllByRole("rowheader");
    expect(rowHeaders.length).toBe(dimensionOrder.length);
  });

  it("each cell exposes a non-visual support label for screen readers", () => {
    const { getByRole } = render(
      <ComparisonTable competitors={["tbd", "ynab"]} />,
    );
    // sr-only text like "Yes" / "No" / "Partial" appears in cells
    expect(within(getByRole("table")).getAllByText(/^(Yes|No|Partial)$/).length)
      .toBeGreaterThan(0);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run tests/comparison-table.test.tsx`
Expected: FAIL — cannot resolve `@/components/landing/ComparisonTable`.

- [ ] **Step 3: Write the component**

```tsx
// frontend/components/landing/ComparisonTable.tsx
// Accessible comparison table. Token-only colors (no raw palette; CI-checked).
// Cells carry a short factual phrase AND a non-visual support label, so meaning
// never rides on color or glyph alone (WCAG 2.2 AA). Server-rendered, no client JS.
import {
  type Competitor,
  comparisonMatrix,
  competitorMeta,
  dimensionLabels,
  dimensionOrder,
} from "@/lib/comparison";

const supportLabel: Record<"yes" | "no" | "partial", string> = {
  yes: "Yes",
  no: "No",
  partial: "Partial",
};

function SupportGlyph({ supported }: { supported: "yes" | "no" | "partial" }) {
  const path =
    supported === "yes"
      ? "M3.5 8.5l3 3 6-7"
      : supported === "no"
        ? "M4 8h8"
        : "M3.5 9.5c2-3 5 3 9 0";
  return (
    <svg viewBox="0 0 16 16" aria-hidden className="h-4 w-4 flex-shrink-0">
      <path
        d={path}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export default function ComparisonTable({
  competitors,
}: {
  competitors: ReadonlyArray<Competitor>;
}) {
  return (
    <div className="overflow-x-auto rounded-xl border border-border bg-surface">
      <table className="w-full border-collapse text-left text-sm">
        <thead>
          <tr className="border-b border-border">
            <th scope="col" className="px-4 py-3 font-medium text-text-muted">
              How they compare
            </th>
            {competitors.map((c) => (
              <th
                key={c}
                scope="col"
                className="px-4 py-3 font-display font-semibold text-text-primary"
              >
                {competitorMeta[c].name}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {dimensionOrder.map((dim) => (
            <tr key={dim} className="border-b border-border last:border-0">
              <th
                scope="row"
                className="px-4 py-3 align-top font-medium text-text-primary"
              >
                {dimensionLabels[dim]}
              </th>
              {competitors.map((c) => {
                const cell = comparisonMatrix[dim][c];
                return (
                  <td
                    key={c}
                    className="px-4 py-3 align-top text-text-secondary"
                  >
                    <span className="flex items-start gap-2">
                      <span className="mt-0.5 text-text-muted">
                        <SupportGlyph supported={cell.supported} />
                        <span className="sr-only">
                          {supportLabel[cell.supported]}
                        </span>
                      </span>
                      <span>{cell.value}</span>
                    </span>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run tests/comparison-table.test.tsx`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/components/landing/ComparisonTable.tsx frontend/tests/comparison-table.test.tsx
git commit -m "feat(landing): accessible ComparisonTable primitive"
```

## Task 3: VsPageLayout primitive (skeleton + JSON-LD)

**Files:**
- Create: `frontend/components/landing/VsPageLayout.tsx`
- Test: `frontend/tests/vs-page-jsonld.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/tests/vs-page-jsonld.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render, within } from "@testing-library/react";
import VsPageLayout from "@/components/landing/VsPageLayout";

vi.mock("@/lib/links", () => ({
  signupHref: () => "/register",
  ctaHref: (p: string) => p,
  IS_APEX_BUILD: false,
}));

describe("VsPageLayout", () => {
  const faq = [
    { q: "Is it a good YNAB alternative?", a: "Yes, if you want forecasting." },
    { q: "Does it sync with my bank?", a: "No, it imports CSV or OFX." },
  ];

  it("emits a FAQPage and BreadcrumbList JSON-LD mirroring the faq prop", () => {
    const { container } = render(
      <VsPageLayout
        slug="ynab"
        competitor="ynab"
        title="The Better Decision vs YNAB"
        intro={<p>Intro copy.</p>}
        faq={faq}
        nonce=""
      />,
    );
    const parsed = Array.from(
      container.querySelectorAll('script[type="application/ld+json"]'),
    ).map((s) => JSON.parse(s.textContent ?? "{}"));
    const types = parsed.map((p) => p["@type"]);
    expect(types).toContain("FAQPage");
    expect(types).toContain("BreadcrumbList");
    const faqLd = parsed.find((p) => p["@type"] === "FAQPage");
    expect(faqLd.mainEntity.length).toBe(2);
    expect(faqLd.mainEntity[0]["@type"]).toBe("Question");
    expect(faqLd.mainEntity[0].acceptedAnswer["@type"]).toBe("Answer");
  });

  it("renders the honest 'where they win' points from comparison data", () => {
    const { getByRole } = render(
      <VsPageLayout
        slug="ynab"
        competitor="ynab"
        title="The Better Decision vs YNAB"
        intro={<p>Intro copy.</p>}
        faq={faq}
        nonce=""
      />,
    );
    const region = getByRole("region", { name: /where ynab wins/i });
    expect(within(region).getByText(/live bank sync/i)).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run tests/vs-page-jsonld.test.tsx`
Expected: FAIL — cannot resolve `@/components/landing/VsPageLayout`.

- [ ] **Step 3: Write the component**

```tsx
// frontend/components/landing/VsPageLayout.tsx
// Shared skeleton for every /vs/<x> page. The page passes its DISTINCT content
// (title, intro JSX, FAQ) plus the competitor slug; this layout pulls the table
// slice and the honest "where they win" points from the single comparison data
// source and assembles JSON-LD. Pages stay structurally consistent; their
// content stays distinct (so the cluster is not thin/doorway).
import Link from "next/link";
import type { ReactNode } from "react";
import type { FaqEntry } from "./faqData";
import ComparisonTable from "./ComparisonTable";
import { type Competitor, competitorMeta } from "@/lib/comparison";
import { btnPrimary } from "@/lib/styles";
import { signupHref } from "@/lib/links";
import { apexCanonical, apexUrl } from "@/lib/site";

export default function VsPageLayout({
  slug,
  competitor,
  title,
  intro,
  faq,
  nonce,
}: {
  slug: string;
  competitor: Exclude<Competitor, "tbd">;
  title: string;
  intro: ReactNode;
  faq: ReadonlyArray<{ q: string; a: string } & Partial<FaqEntry>>;
  nonce: string;
}) {
  const nonceProp = nonce ? { nonce } : {};
  const meta = competitorMeta[competitor];

  const faqLd = {
    "@context": "https://schema.org",
    "@type": "FAQPage",
    mainEntity: faq.map((f) => ({
      "@type": "Question",
      name: f.q,
      acceptedAnswer: { "@type": "Answer", text: f.a },
    })),
  };
  const breadcrumbLd = {
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    itemListElement: [
      { "@type": "ListItem", position: 1, name: "Home", item: apexCanonical("/") },
      { "@type": "ListItem", position: 2, name: "Compare", item: apexCanonical("/compare") },
      { "@type": "ListItem", position: 3, name: meta.name, item: apexCanonical(`/vs/${slug}`) },
    ],
  };
  const structuredData = [faqLd, breadcrumbLd];

  return (
    <main className="mx-auto max-w-3xl px-6 py-20 lg:py-24">
      {structuredData.map((block) => (
        <script
          key={block["@type"]}
          type="application/ld+json"
          {...nonceProp}
          dangerouslySetInnerHTML={{
            __html: JSON.stringify(block).replace(/<\/script>/gi, "<\\/script>"),
          }}
        />
      ))}

      <h1 className="font-display text-3xl font-semibold leading-tight text-text-primary lg:text-4xl">
        {title}
      </h1>
      <div className="mt-4 space-y-4 text-base leading-relaxed text-text-secondary">
        {intro}
      </div>

      <section aria-label="Feature comparison" className="mt-12">
        <ComparisonTable competitors={["tbd", competitor]} />
      </section>

      <section
        aria-label={`Where ${meta.name} wins`}
        className="mt-12 rounded-xl border border-border bg-surface p-6"
      >
        <h2 className="font-display text-xl font-semibold text-text-primary">
          Where {meta.name} wins
        </h2>
        <p className="mt-2 text-sm text-text-muted">
          No tool wins every row. Here is where {meta.name} is the better choice.
        </p>
        <ul className="mt-4 list-disc space-y-2 pl-5 text-sm leading-relaxed text-text-secondary">
          {meta.whereTheyWin.map((point) => (
            <li key={point}>{point}</li>
          ))}
        </ul>
      </section>

      <section aria-label="Common questions" className="mt-12">
        <h2 className="font-display text-xl font-semibold text-text-primary">
          Common questions
        </h2>
        <ul className="mt-4 space-y-3">
          {faq.map((item) => (
            <li key={item.q} className="rounded-xl border border-border bg-surface">
              <details className="group">
                <summary className="flex cursor-pointer list-none items-center justify-between gap-4 rounded-xl px-5 py-4 text-left text-sm font-medium text-text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40">
                  <span>{item.q}</span>
                </summary>
                <div className="border-t border-border px-5 py-4 text-sm leading-relaxed text-text-secondary">
                  {item.a}
                </div>
              </details>
            </li>
          ))}
        </ul>
      </section>

      <section className="mt-12 text-center">
        <p className="text-base text-text-secondary">
          See your money clearly and plan what is coming. Free while in beta.
        </p>
        <Link href={signupHref()} className={`${btnPrimary} mt-4 inline-flex items-center`}>
          Get started
        </Link>
        <p className="mt-6 text-sm text-text-muted">
          <Link href="/compare" className="underline hover:text-text-primary">
            Compare all options
          </Link>{" "}
          ·{" "}
          <Link href="/features" className="underline hover:text-text-primary">
            See every feature
          </Link>
        </p>
      </section>
    </main>
  );
}
```

> Note: `apexUrl` import is intentionally available for future entity JSON-LD; if lint flags it as unused, drop it from the import line.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run tests/vs-page-jsonld.test.tsx`
Expected: PASS (2 tests).

- [ ] **Step 5: Run the full suite + typecheck, then commit**

Run: `cd frontend && npm test && npx tsc --noEmit`
Expected: all green.

```bash
git add frontend/components/landing/VsPageLayout.tsx frontend/tests/vs-page-jsonld.test.tsx
git commit -m "feat(landing): VsPageLayout primitive with comparison JSON-LD"
```

- [ ] **Step 6: Open PR**

```bash
git push -u origin feat/comparison-primitives
gh pr create --title "feat(landing): comparison data model and primitives" --body "Adds lib/comparison.ts (single source of truth), ComparisonTable, and VsPageLayout with tests. No routes yet."
```

---

# PR 2 — /features and /compare routes + full two-host plumbing

Branch: `feat/features-compare-pages` (off `feat/comparison-primitives` or main after PR 1 merges). Proves the entire two-host pipeline on two routes before multiplying by four.

## Task 4: `/compare` page (canonical full matrix + spoke hub)

**Files:**
- Create: `frontend/app/compare/page.tsx`

- [ ] **Step 1: Write the page**

```tsx
// frontend/app/compare/page.tsx
import Link from "next/link";
import type { Metadata } from "next";
import { readNonce } from "@/lib/nonce";
import { apexCanonical, apexUrl, pageSocialMeta, siteName } from "@/lib/site";
import ComparisonTable from "@/components/landing/ComparisonTable";
import { competitorOrder } from "@/lib/comparison";

const description =
  "Compare The Better Decision with YNAB, PocketSmith, Monarch, and spreadsheets on forecasting, budgeting, bank sync, EU data residency, and price.";

export const metadata: Metadata = {
  title: "Compare budgeting and forecasting apps",
  description,
  alternates: { canonical: apexCanonical("/compare") },
  ...pageSocialMeta({
    title: `Compare budgeting and forecasting apps · ${siteName}`,
    description,
    path: apexCanonical("/compare"),
  }),
  robots: { index: true, follow: true },
};

const orgId = `${apexUrl}/#organization`;
const websiteId = `${apexUrl}/#website`;

const breadcrumbLd = {
  "@context": "https://schema.org",
  "@type": "BreadcrumbList",
  itemListElement: [
    { "@type": "ListItem", position: 1, name: "Home", item: apexCanonical("/") },
    { "@type": "ListItem", position: 2, name: "Compare", item: apexCanonical("/compare") },
  ],
};
const faqLd = {
  "@context": "https://schema.org",
  "@type": "FAQPage",
  mainEntity: [
    {
      "@type": "Question",
      name: "What is the best app for budgeting and cash-flow forecasting?",
      acceptedAnswer: {
        "@type": "Answer",
        text: "It depends on what you need. The Better Decision combines budgeting with forward-looking cash-flow forecasting, EU-hosted, and imports CSV or OFX rather than linking your bank. YNAB is strongest for strict envelope budgeting, PocketSmith for deep long-range forecasting, and Monarch for live bank and investment aggregation.",
      },
    },
    {
      "@type": "Question",
      name: "Which budgeting apps host data in the EU?",
      acceptedAnswer: {
        "@type": "Answer",
        text: "The Better Decision is EU-hosted and processed under EU law. PocketSmith is based in New Zealand, which has an EU adequacy decision. YNAB and Monarch are US-based.",
      },
    },
  ],
};
const structuredData = [breadcrumbLd, faqLd, { "@id": orgId, "@id2": websiteId }].slice(0, 2);

export default async function ComparePage() {
  const nonce = await readNonce();
  const nonceProp = nonce ? { nonce } : {};
  return (
    <main className="mx-auto max-w-5xl px-6 py-20 lg:py-24">
      {structuredData.map((block) => (
        <script
          key={(block as { "@type": string })["@type"]}
          type="application/ld+json"
          {...nonceProp}
          dangerouslySetInnerHTML={{
            __html: JSON.stringify(block).replace(/<\/script>/gi, "<\\/script>"),
          }}
        />
      ))}
      <h1 className="font-display text-3xl font-semibold leading-tight text-text-primary lg:text-4xl">
        How The Better Decision compares
      </h1>
      <p className="mt-4 max-w-2xl text-base leading-relaxed text-text-secondary">
        The Better Decision is a budgeting and cash-flow forecasting app, EU-hosted,
        that imports your CSV or OFX files instead of linking your bank. Here is how it
        stacks up against the tools people switch from. No tool wins every row, and the
        table says so.
      </p>
      <div className="mt-10">
        <ComparisonTable competitors={competitorOrder} />
      </div>
      <p className="mt-10 text-sm text-text-muted">
        Read the detailed comparisons:{" "}
        <Link href="/vs/spreadsheets" className="underline hover:text-text-primary">
          vs spreadsheets
        </Link>{" "}
        ·{" "}
        <Link href="/vs/ynab" className="underline hover:text-text-primary">
          vs YNAB
        </Link>
        .
      </p>
    </main>
  );
}
```

> The `structuredData` line above is deliberately written to render exactly the breadcrumb + FAQ blocks. Simplify to `const structuredData = [breadcrumbLd, faqLd];` — the `.slice` form is only a guard against accidental extra entries; prefer the plain array:
>
> ```ts
> const structuredData = [breadcrumbLd, faqLd];
> ```
>
> Use the plain-array form. (When implementing, replace the `.slice(0,2)` line with the plain array and remove the unused `orgId`/`websiteId` if lint complains.)

- [ ] **Step 2: Typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add frontend/app/compare/page.tsx
git commit -m "feat(landing): /compare canonical comparison matrix page"
```

## Task 5: `/features` page (hub + shipped AI group + roadmap block)

**Files:**
- Create: `frontend/app/features/page.tsx`

- [ ] **Step 1: Write the page**

```tsx
// frontend/app/features/page.tsx
import Link from "next/link";
import type { Metadata } from "next";
import { readNonce } from "@/lib/nonce";
import { apexCanonical, apexUrl, pageSocialMeta, siteName } from "@/lib/site";

const description =
  "Everything The Better Decision does: cash-flow forecasting, budgets, recurring bills, imports, reports, household sharing, and bring-your-own or local AI, all EU-hosted.";

export const metadata: Metadata = {
  title: "Features",
  description,
  alternates: { canonical: apexCanonical("/features") },
  ...pageSocialMeta({
    title: `Features · ${siteName}`,
    description,
    path: apexCanonical("/features"),
  }),
  robots: { index: true, follow: true },
};

const orgId = `${apexUrl}/#organization`;

// featureList holds ONLY shipped features. Roadmap items are excluded by design
// (see the roadmap block below and the no-roadmap-in-featurelist test).
const shippedFeatures = [
  "Cash-flow forecasting with what-if scenarios",
  "Projected end-of-month balance per account",
  "Category budgets and forecast plans",
  "Recurring income and bills",
  "CSV and OFX import with a preview before anything is saved",
  "Reports by category",
  "Shared household organization with roles",
  "EU-hosted, export anytime, never used to train AI",
  "Optional AI: bring your own key or run it locally with Ollama, with spend caps and an audit trail",
];

const softwareLd = {
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  name: siteName,
  applicationCategory: "FinanceApplication",
  operatingSystem: "Web",
  url: apexCanonical("/features"),
  author: { "@id": orgId },
  publisher: { "@id": orgId },
  featureList: shippedFeatures,
};
const breadcrumbLd = {
  "@context": "https://schema.org",
  "@type": "BreadcrumbList",
  itemListElement: [
    { "@type": "ListItem", position: 1, name: "Home", item: apexCanonical("/") },
    { "@type": "ListItem", position: 2, name: "Features", item: apexCanonical("/features") },
  ],
};
const structuredData = [softwareLd, breadcrumbLd];

const groups = [
  {
    title: "See clearly",
    points: [
      "Auto-categorization that learns from your edits.",
      "Import CSV or OFX from your bank, with a preview before anything is written.",
      "Reports that group spending by category.",
    ],
  },
  {
    title: "Plan what is coming",
    points: [
      "Forecast plans with what-if scenarios and a projected end-of-month balance.",
      "Recurring income and bills that roll forward automatically.",
      "Category budgets for the current period.",
    ],
  },
  {
    title: "Together, if you want",
    points: ["One organization, multiple people, clear roles and boundaries."],
  },
  {
    title: "Yours",
    points: [
      "EU-hosted and processed under EU law.",
      "Export your data anytime. It is never sold and never used to train AI.",
      "Delete your account, and your data, in one click.",
    ],
  },
  {
    title: "AI on your terms",
    points: [
      "Suggest a category, refine a forecast with seasonal patterns, or rebalance a budget. You accept or reject every suggestion before anything is saved.",
      "Bring your own OpenAI or Anthropic key, or run it entirely locally with Ollama.",
      "Hard and soft spend caps, plus a full audit trail of every call.",
    ],
  },
];

export default async function FeaturesPage() {
  const nonce = await readNonce();
  const nonceProp = nonce ? { nonce } : {};
  return (
    <main className="mx-auto max-w-4xl px-6 py-20 lg:py-24">
      {structuredData.map((block) => (
        <script
          key={block["@type"]}
          type="application/ld+json"
          {...nonceProp}
          dangerouslySetInnerHTML={{
            __html: JSON.stringify(block).replace(/<\/script>/gi, "<\\/script>"),
          }}
        />
      ))}
      <h1 className="font-display text-3xl font-semibold leading-tight text-text-primary lg:text-4xl">
        Everything in The Better Decision
      </h1>
      <p className="mt-4 max-w-2xl text-base leading-relaxed text-text-secondary">
        Budgeting and cash-flow forecasting in one calm app, EU-hosted, for normal
        people. Here is what it does today.
      </p>

      <div className="mt-12 grid gap-6 md:grid-cols-2">
        {groups.map((g) => (
          <section key={g.title} className="rounded-xl border border-border bg-surface p-6">
            <h2 className="font-display text-lg font-semibold text-text-primary">{g.title}</h2>
            <ul className="mt-3 list-disc space-y-2 pl-5 text-sm leading-relaxed text-text-secondary">
              {g.points.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          </section>
        ))}
      </div>

      <section
        aria-label="On the roadmap"
        className="mt-12 rounded-xl border border-dashed border-border bg-surface p-6"
      >
        <p className="text-xs font-semibold uppercase tracking-[0.12em] text-text-muted">
          On the roadmap
        </p>
        <h2 className="mt-2 font-display text-lg font-semibold text-text-primary">
          Not built yet, but coming
        </h2>
        <ul className="mt-3 list-disc space-y-2 pl-5 text-sm leading-relaxed text-text-secondary">
          <li>
            Connect your finances to your own AI assistant over MCP, with every change
            confirmed before it runs.
          </li>
          <li>A hosted AI option, opt-in and consent-gated, as an alternative to your own key.</li>
        </ul>
      </section>

      <p className="mt-12 text-sm text-text-muted">
        Wondering how it stacks up?{" "}
        <Link href="/compare" className="underline hover:text-text-primary">
          Compare The Better Decision with YNAB, PocketSmith, Monarch, and spreadsheets
        </Link>
        .
      </p>
    </main>
  );
}
```

- [ ] **Step 2: Typecheck + commit**

Run: `cd frontend && npx tsc --noEmit`
Expected: PASS.

```bash
git add frontend/app/features/page.tsx
git commit -m "feat(landing): /features hub with shipped AI group and roadmap block"
```

## Task 6: Two-host SEO plumbing for /features and /compare

**Files:**
- Modify: `frontend/app/sitemap.ts`
- Modify: `frontend/app/robots.ts`
- Modify: `frontend/scripts/build-apex.sh:47-51` and `:74-105` and `:294-303`
- Modify: `frontend/public/llms.txt`
- Test: `frontend/tests/seo-public-routes-indexable.test.tsx`, `frontend/tests/build-apex.test.ts`

- [ ] **Step 1: Extend the indexable test (failing)**

Add to `frontend/tests/seo-public-routes-indexable.test.tsx` after the existing docs imports/entries:

```tsx
import { metadata as featuresMetadata } from "@/app/features/page";
import { metadata as compareMetadata } from "@/app/compare/page";
```
and add to the `indexableMetadatas` array:
```tsx
  ["/features", featuresMetadata],
  ["/compare", compareMetadata],
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd frontend && npx vitest run tests/seo-public-routes-indexable.test.tsx`
Expected: PASS only if pages already export `robots:{index:true}` — they do (Tasks 4-5). If a page is missing the metadata export it FAILS here. (This test guards the easy-to-forget `robots` export.)

- [ ] **Step 3: Edit `app/sitemap.ts`**

Add these entries to the returned array (after the `/terms` entry, before `/docs`):

```ts
    {
      url: `${siteUrl}/features`,
      lastModified,
      changeFrequency: "monthly",
      priority: 0.7,
    },
    {
      url: `${siteUrl}/compare`,
      lastModified,
      changeFrequency: "monthly",
      priority: 0.7,
    },
```

- [ ] **Step 4: Edit `app/robots.ts`**

Change the `allow` array to include the new public routes:

```ts
        allow: ["/", "/login", "/register", "/privacy", "/terms", "/docs", "/docs/plans", "/features", "/compare", "/vs"],
```

(`/vs` is added now so the four child pages are allowed when they land in PR 3; only the indexed ones appear in the sitemap.)

- [ ] **Step 5: Edit `scripts/build-apex.sh`**

In `ALLOWED_ROUTE_DIRS` (lines 47-51) add `"features"`, `"compare"`, `"vs"`:

```bash
ALLOWED_ROUTE_DIRS=(
  "privacy"
  "terms"
  "docs"
  "features"
  "compare"
  "vs"
)
```

In `ALLOWED_OUTPUT_GLOBS` (lines 74-105) add `"features"`, `"compare"`, `"vs"` near the other route dirs (after `"docs"`):

```bash
  "docs"
  "features"
  "compare"
  "vs"
```

In the apex sitemap heredoc (lines 294-303) add the published routes after the `/docs/plans/` line:

```bash
  <url><loc>${APEX_URL}/features/</loc><lastmod>${BUILD_TIME%T*}</lastmod></url>
  <url><loc>${APEX_URL}/compare/</loc><lastmod>${BUILD_TIME%T*}</lastmod></url>
  <url><loc>${APEX_URL}/vs/spreadsheets/</loc><lastmod>${BUILD_TIME%T*}</lastmod></url>
  <url><loc>${APEX_URL}/vs/ynab/</loc><lastmod>${BUILD_TIME%T*}</lastmod></url>
```

(PocketSmith and Monarch heredoc lines are added in the publish PR, Task 12.)

- [ ] **Step 6: Edit `public/llms.txt`**

Under `## Pages`, after the `Home` line, add:

```
- [Features](https://thebetterdecision.com/features/): Everything the app does, including bring-your-own and local AI, plus what is on the roadmap.
- [Compare](https://thebetterdecision.com/compare/): How The Better Decision compares with YNAB, PocketSmith, Monarch, and spreadsheets.
- [vs Spreadsheets](https://thebetterdecision.com/vs/spreadsheets/): Why a forecasting app beats a manual budgeting spreadsheet, and where spreadsheets still win.
- [vs YNAB](https://thebetterdecision.com/vs/ynab/): A YNAB alternative built around forecasting and EU data residency, and where YNAB still wins.
```

- [ ] **Step 7: Extend `tests/build-apex.test.ts` (assertions for new entries)**

Add a test block (mirror the existing `/docs/plans` heredoc assertion style):

```ts
import { readFileSync } from "node:fs";
import { join } from "node:path";

describe("apex build allowlist covers new marketing routes", () => {
  const script = readFileSync(
    join(__dirname, "..", "scripts", "build-apex.sh"),
    "utf8",
  );
  it.each(["features", "compare", "vs"])("ALLOWED_ROUTE_DIRS includes %s", (d) => {
    expect(script).toMatch(new RegExp(`ALLOWED_ROUTE_DIRS=\\([^)]*"${d}"`, "s"));
  });
  it.each(["features", "compare", "vs"])("ALLOWED_OUTPUT_GLOBS includes %s", (d) => {
    expect(script).toMatch(new RegExp(`ALLOWED_OUTPUT_GLOBS=\\([^)]*"${d}"`, "s"));
  });
  it.each(["/features/", "/compare/", "/vs/spreadsheets/", "/vs/ynab/"])(
    "apex sitemap heredoc lists %s",
    (route) => {
      expect(script).toContain(`${"${APEX_URL}"}${route}</loc>`);
    },
  );
});
```

> If `build-apex.test.ts` already reads the script into a variable, reuse that and only add the `describe` block.

- [ ] **Step 8: Run the full suite + typecheck**

Run: `cd frontend && npm test && npx tsc --noEmit`
Expected: all green (indexable test now covers /features + /compare; build-apex assertions pass).

- [ ] **Step 9: Verify the apex build actually exports both routes**

Run: `cd frontend && npm run build:apex`
Expected: build succeeds, post-build guard passes, and `out-apex/features/index.html` + `out-apex/compare/index.html` exist.
Run: `ls frontend/out-apex/features/index.html frontend/out-apex/compare/index.html`
Expected: both paths listed (no "No such file").

- [ ] **Step 10: Commit + PR**

```bash
git add frontend/app/sitemap.ts frontend/app/robots.ts frontend/scripts/build-apex.sh frontend/public/llms.txt frontend/tests/seo-public-routes-indexable.test.tsx frontend/tests/build-apex.test.ts
git commit -m "feat(landing): two-host SEO plumbing for features and compare"
git push -u origin feat/features-compare-pages
gh pr create --title "feat(landing): /features and /compare pages" --body "Adds the /features hub (shipped AI group + roadmap block) and the /compare matrix, with full two-host SEO plumbing (sitemaps, robots, apex allowlist + output globs + heredoc, llms.txt) and tests. Apex build verified locally."
```

---

# PR 3 — The four /vs pages

Branch: `feat/vs-comparison-pages`. Spreadsheets + YNAB are indexed and linked; PocketSmith + Monarch ship as code, `noindex`, and unlinked (published in Task 12). Each page provides its own distinct intro and FAQ; the table slice and "where they win" come from `lib/comparison.ts`.

## Task 7: `/vs/spreadsheets` (indexed)

**Files:**
- Create: `frontend/app/vs/spreadsheets/page.tsx`

- [ ] **Step 1: Write the page**

```tsx
// frontend/app/vs/spreadsheets/page.tsx
import type { Metadata } from "next";
import { readNonce } from "@/lib/nonce";
import { apexCanonical, pageSocialMeta, siteName } from "@/lib/site";
import VsPageLayout from "@/components/landing/VsPageLayout";

const description =
  "Budget without spreadsheets. The Better Decision forecasts your cash flow automatically, with no formulas to maintain. Here is the honest comparison.";

export const metadata: Metadata = {
  title: "The Better Decision vs spreadsheets: budget without a spreadsheet",
  description,
  alternates: { canonical: apexCanonical("/vs/spreadsheets") },
  ...pageSocialMeta({
    title: `vs Spreadsheets · ${siteName}`,
    description,
    path: apexCanonical("/vs/spreadsheets"),
  }),
  robots: { index: true, follow: true },
};

const faq = [
  {
    q: "Can The Better Decision replace my budgeting spreadsheet?",
    a: "For most people, yes. It imports your CSV or OFX, categorizes transactions, and forecasts your end-of-month balance automatically, without formulas to maintain.",
  },
  {
    q: "Do I lose the flexibility of a spreadsheet?",
    a: "You trade some flexibility for automation. A spreadsheet can model anything you can express in a formula. If you have an unusual calculation, a spreadsheet still wins.",
  },
  {
    q: "Is it free?",
    a: "It is free while in beta. A spreadsheet is free forever, which the comparison says plainly.",
  },
];

export default async function VsSpreadsheetsPage() {
  const nonce = await readNonce();
  return (
    <VsPageLayout
      slug="spreadsheets"
      competitor="spreadsheets"
      title="The Better Decision vs spreadsheets"
      faq={faq}
      nonce={nonce}
      intro={
        <>
          <p>
            A spreadsheet shows you what you typed. The Better Decision shows you what is
            coming: recurring bills and income roll forward automatically into a projected
            end-of-month balance, and imported transactions reconcile against your plan
            without you maintaining a single formula.
          </p>
          <p>
            If you are looking to budget without a spreadsheet but keep the control, this is
            the honest trade-off, including where a spreadsheet still wins.
          </p>
        </>
      }
    />
  );
}
```

- [ ] **Step 2: Typecheck + commit**

Run: `cd frontend && npx tsc --noEmit`
Expected: PASS.

```bash
git add frontend/app/vs/spreadsheets/page.tsx
git commit -m "feat(landing): /vs/spreadsheets comparison page"
```

## Task 8: `/vs/ynab` (indexed)

**Files:**
- Create: `frontend/app/vs/ynab/page.tsx`

- [ ] **Step 1: Write the page**

```tsx
// frontend/app/vs/ynab/page.tsx
import type { Metadata } from "next";
import { readNonce } from "@/lib/nonce";
import { apexCanonical, pageSocialMeta, siteName } from "@/lib/site";
import VsPageLayout from "@/components/landing/VsPageLayout";

const description =
  "A YNAB alternative built around forecasting and EU data residency. The Better Decision adds the forward view YNAB leaves out, and concedes where YNAB still wins.";

export const metadata: Metadata = {
  title: "A YNAB alternative built around forecasting",
  description,
  alternates: { canonical: apexCanonical("/vs/ynab") },
  ...pageSocialMeta({
    title: `vs YNAB · ${siteName}`,
    description,
    path: apexCanonical("/vs/ynab"),
  }),
  robots: { index: true, follow: true },
};

const faq = [
  {
    q: "Is The Better Decision a good YNAB alternative?",
    a: "Yes, if you want a forward-looking projected balance and EU data residency. It adds cash-flow forecasting that YNAB does not do. It does not have YNAB's live bank sync.",
  },
  {
    q: "Does it use the envelope method like YNAB?",
    a: "No. YNAB is built around zero-based envelopes for the current month. The Better Decision focuses on category budgets plus a forward forecast across periods.",
  },
  {
    q: "Does it sync with my bank like YNAB?",
    a: "No. The Better Decision imports CSV or OFX files. Nothing connects to your bank automatically, which some people prefer.",
  },
];

export default async function VsYnabPage() {
  const nonce = await readNonce();
  return (
    <VsPageLayout
      slug="ynab"
      competitor="ynab"
      title="The Better Decision vs YNAB"
      faq={faq}
      nonce={nonce}
      intro={
        <>
          <p>
            Looking for a YNAB alternative? YNAB is a zero-based budgeting method where every
            dollar gets a job, this month. The Better Decision adds the forward view YNAB
            deliberately leaves out: a projected balance across your accounts weeks and months
            ahead, with what-if scenarios, EU-hosted.
          </p>
          <p>
            YNAB is a strong product with a loyal community, so this comparison is fair about
            where it still wins.
          </p>
        </>
      }
    />
  );
}
```

- [ ] **Step 2: Typecheck + commit**

Run: `cd frontend && npx tsc --noEmit`
Expected: PASS.

```bash
git add frontend/app/vs/ynab/page.tsx
git commit -m "feat(landing): /vs/ynab comparison page"
```

## Task 9: `/vs/pocketsmith` (noindex until publish)

**Files:**
- Create: `frontend/app/vs/pocketsmith/page.tsx`

- [ ] **Step 1: Write the page** (note `robots: { index: false, follow: false }`)

```tsx
// frontend/app/vs/pocketsmith/page.tsx
// NOTE: noindex until the publish PR (staggered launch). Do NOT add to sitemap,
// llms.txt, or internal links until then. Honesty: PocketSmith's headline is
// forecasting and it is NZ (EU adequacy), so this page leads with simplicity and
// household sharing, NOT "we forecast better" or a hard EU-privacy claim.
import type { Metadata } from "next";
import { readNonce } from "@/lib/nonce";
import { apexCanonical, pageSocialMeta, siteName } from "@/lib/site";
import VsPageLayout from "@/components/landing/VsPageLayout";

const description =
  "The Better Decision brings cash-flow forecasting into a simpler, calmer, household-shared app. Here is the honest comparison with PocketSmith.";

export const metadata: Metadata = {
  title: "The Better Decision vs PocketSmith",
  description,
  alternates: { canonical: apexCanonical("/vs/pocketsmith") },
  ...pageSocialMeta({
    title: `vs PocketSmith · ${siteName}`,
    description,
    path: apexCanonical("/vs/pocketsmith"),
  }),
  robots: { index: false, follow: false },
};

const faq = [
  {
    q: "Does PocketSmith forecast better than The Better Decision?",
    a: "PocketSmith offers deeper, longer-range forecasting with more scenarios and multi-currency. The Better Decision brings the core forward view into a simpler, calmer app.",
  },
  {
    q: "Is The Better Decision more private than PocketSmith?",
    a: "Both treat your data seriously. The Better Decision is EU-hosted and processed under EU law; PocketSmith is based in New Zealand, which has an EU adequacy decision. The difference is locality preference, not safety.",
  },
  {
    q: "Which is simpler to use?",
    a: "The Better Decision is designed for normal people and households, with less setup than PocketSmith's power-user depth.",
  },
];

export default async function VsPocketsmithPage() {
  const nonce = await readNonce();
  return (
    <VsPageLayout
      slug="pocketsmith"
      competitor="pocketsmith"
      title="The Better Decision vs PocketSmith"
      faq={faq}
      nonce={nonce}
      intro={
        <>
          <p>
            PocketSmith pioneered long-range cash-flow forecasting. The Better Decision brings
            that same forward view into a simpler, calmer, household-shared app, without
            spreadsheet-grade complexity, and hosts your data in the EU.
          </p>
          <p>
            PocketSmith forecasts deeper than we do today, and this comparison says so plainly.
          </p>
        </>
      }
    />
  );
}
```

- [ ] **Step 2: Typecheck + commit**

Run: `cd frontend && npx tsc --noEmit`
Expected: PASS.

```bash
git add frontend/app/vs/pocketsmith/page.tsx
git commit -m "feat(landing): /vs/pocketsmith comparison page (noindex until launch)"
```

## Task 10: `/vs/monarch` (noindex until publish) + the noindex/JSON-LD test

**Files:**
- Create: `frontend/app/vs/monarch/page.tsx`
- Test: `frontend/tests/seo-public-routes-indexable.test.tsx`

- [ ] **Step 1: Write the page** (note `robots: { index: false, follow: false }`)

```tsx
// frontend/app/vs/monarch/page.tsx
// NOTE: noindex until the publish PR (staggered launch).
import type { Metadata } from "next";
import { readNonce } from "@/lib/nonce";
import { apexCanonical, pageSocialMeta, siteName } from "@/lib/site";
import VsPageLayout from "@/components/landing/VsPageLayout";

const description =
  "The Better Decision leads with planning and EU data residency, with no bank linking required. Here is the honest comparison with Monarch.";

export const metadata: Metadata = {
  title: "The Better Decision vs Monarch",
  description,
  alternates: { canonical: apexCanonical("/vs/monarch") },
  ...pageSocialMeta({
    title: `vs Monarch · ${siteName}`,
    description,
    path: apexCanonical("/vs/monarch"),
  }),
  robots: { index: false, follow: false },
};

const faq = [
  {
    q: "Is The Better Decision a Monarch alternative?",
    a: "Yes, if you want forecasting-first planning and EU data residency, and you prefer not to auto-link every account. Monarch's strength is comprehensive live bank and investment aggregation, which The Better Decision does not do.",
  },
  {
    q: "Does it track net worth like Monarch?",
    a: "Not in the same automated way. Monarch links accounts to track net worth. The Better Decision imports CSV or OFX and focuses on cash-flow planning.",
  },
  {
    q: "Where is my data stored?",
    a: "In the EU, processed under EU law. Monarch is US-based.",
  },
];

export default async function VsMonarchPage() {
  const nonce = await readNonce();
  return (
    <VsPageLayout
      slug="monarch"
      competitor="monarch"
      title="The Better Decision vs Monarch"
      faq={faq}
      nonce={nonce}
      intro={
        <>
          <p>
            Monarch is a polished US tracking and net-worth app built on bank aggregation. The
            Better Decision leads with planning: a projected balance and what-if scenarios,
            EU-hosted, with no account linking required.
          </p>
          <p>
            Monarch's automatic aggregation is genuinely strong, and this comparison concedes it.
          </p>
        </>
      }
    />
  );
}
```

- [ ] **Step 2: Add the noindex assertions test (failing first)**

Append to `frontend/tests/seo-public-routes-indexable.test.tsx`:

```tsx
import { metadata as vsSpreadsheetsMetadata } from "@/app/vs/spreadsheets/page";
import { metadata as vsYnabMetadata } from "@/app/vs/ynab/page";
import { metadata as vsPocketsmithMetadata } from "@/app/vs/pocketsmith/page";
import { metadata as vsMonarchMetadata } from "@/app/vs/monarch/page";

describe("published /vs pages are indexable", () => {
  it.each([
    ["/vs/spreadsheets", vsSpreadsheetsMetadata],
    ["/vs/ynab", vsYnabMetadata],
  ] as const)("%s indexes", (_r, meta) => {
    expect(meta.robots).toEqual({ index: true, follow: true });
  });
});

describe("staggered /vs pages stay out of the index until launch", () => {
  it.each([
    ["/vs/pocketsmith", vsPocketsmithMetadata],
    ["/vs/monarch", vsMonarchMetadata],
  ] as const)("%s is noindex", (_r, meta) => {
    expect(meta.robots).toEqual({ index: false, follow: false });
  });
});
```

- [ ] **Step 3: Run the full suite + typecheck**

Run: `cd frontend && npm test && npx tsc --noEmit`
Expected: all green. The four `/vs` pages render the FAQPage JSON-LD via `VsPageLayout` (covered by `vs-page-jsonld.test.tsx`); the noindex split is asserted here.

- [ ] **Step 4: Verify the apex build exports all four /vs pages**

Run: `cd frontend && npm run build:apex && ls frontend/out-apex/vs/spreadsheets/index.html frontend/out-apex/vs/ynab/index.html frontend/out-apex/vs/pocketsmith/index.html frontend/out-apex/vs/monarch/index.html`
Expected: all four listed. (PocketSmith/Monarch are exported but carry `<meta name="robots" content="noindex">` and are absent from the sitemap.)

- [ ] **Step 5: Commit + PR**

```bash
git add frontend/app/vs frontend/tests/seo-public-routes-indexable.test.tsx
git commit -m "feat(landing): /vs/monarch page and vs-route index assertions"
git push -u origin feat/vs-comparison-pages
gh pr create --title "feat(landing): vs comparison pages" --body "Four /vs comparison pages on the shared VsPageLayout. Spreadsheets and YNAB are indexed and linked; PocketSmith and Monarch ship noindex and unlinked for a staggered launch. Apex export verified."
```

---

# PR 4 — Homepage forecasting-first content pass

Branch: `feat/homepage-forecasting-pass`.

## Task 11: Hero copy + "Everything in the app" strip + featureList

**Files:**
- Modify: `frontend/components/landing/Hero.tsx` (subcopy)
- Modify: `frontend/app/page.tsx` (insert strip after `ScreenshotShowcase`; add `featureList` to existing `SoftwareApplication` block)
- Create: `frontend/components/landing/EverythingInTheApp.tsx`

- [ ] **Step 1: Create the strip component**

```tsx
// frontend/components/landing/EverythingInTheApp.tsx
// Compact strip surfacing more shipped features on the highest-traffic page,
// linking to /features. Shipped items only, no roadmap. No em-dashes.
import Link from "next/link";

const items = [
  "Cash-flow forecasting and what-if scenarios",
  "Recurring income and bills",
  "Category budgets and reports",
  "CSV and OFX import",
  "Shared household with roles",
  "Bring-your-own or local AI, with spend caps",
];

export default function EverythingInTheApp() {
  return (
    <section
      aria-label="Everything in the app"
      className="mx-auto max-w-6xl px-6 py-20 lg:px-10 lg:py-24"
    >
      <h2 className="font-display text-3xl font-semibold leading-tight text-text-primary lg:text-4xl">
        Everything in the app
      </h2>
      <ul className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {items.map((item) => (
          <li
            key={item}
            className="rounded-xl border border-border bg-surface p-5 text-sm leading-relaxed text-text-secondary"
          >
            {item}
          </li>
        ))}
      </ul>
      <p className="mt-8 text-sm text-text-muted">
        <Link href="/features" className="underline hover:text-text-primary">
          See every feature
        </Link>{" "}
        ·{" "}
        <Link href="/compare" className="underline hover:text-text-primary">
          Compare with YNAB, PocketSmith, Monarch, and spreadsheets
        </Link>
      </p>
    </section>
  );
}
```

- [ ] **Step 2: Insert the strip in `app/page.tsx`**

Import at top: `import EverythingInTheApp from "@/components/landing/EverythingInTheApp";`
Then render it immediately after `<ScreenshotShowcase />` in the page body.

- [ ] **Step 3: Add `featureList` to the homepage SoftwareApplication JSON-LD**

In `app/page.tsx`, locate the `SoftwareApplication` JSON-LD object (the one with `applicationCategory: "FinanceApplication"`). Add a `featureList` key listing shipped features only (mirror the `shippedFeatures` array from `app/features/page.tsx` — keep them consistent):

```ts
  featureList: [
    "Cash-flow forecasting with what-if scenarios",
    "Category budgets and forecast plans",
    "Recurring income and bills",
    "CSV and OFX import",
    "Reports by category",
    "Shared household organization with roles",
    "EU-hosted, never used to train AI",
    "Optional bring-your-own or local AI with spend caps",
  ],
```

- [ ] **Step 4: Sharpen the Hero subcopy in `Hero.tsx`**

Update the Hero's supporting paragraph (the value-prop line under the H1) to lead forecasting, privacy second. Use exactly (no em-dashes):

```
See what is coming, not just what happened. The Better Decision forecasts your cash flow and plans your budget in one calm app, EU-hosted, for normal people.
```

Keep the existing H1 statement and the trust line. Only the supporting paragraph text changes.

- [ ] **Step 5: Add the no-roadmap-in-featureList guard test**

```tsx
// frontend/tests/features-no-roadmap-in-featurelist.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";
import FeaturesPage from "@/app/features/page";
import LandingPage from "@/app/page";

vi.mock("@/components/landing/LandingAuthRedirect", () => ({ default: () => null }));
vi.mock("@/lib/nonce", () => ({ readNonce: async () => "" }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/",
  useSearchParams: () => new URLSearchParams(),
}));

const ROADMAP_TERMS = [/\bMCP\b/i, /assistant/i, /hosted AI/i, /roadmap/i];

async function featureListStrings(Page: () => Promise<React.ReactElement>) {
  const { container } = render((await Page()) as React.ReactElement);
  const blocks = Array.from(
    container.querySelectorAll('script[type="application/ld+json"]'),
  )
    .map((s) => JSON.parse(s.textContent ?? "{}"))
    .filter((b) => Array.isArray(b.featureList));
  return blocks.flatMap((b) => b.featureList as string[]);
}

describe("featureList JSON-LD never advertises roadmap items", () => {
  it("on /features", async () => {
    const list = await featureListStrings(FeaturesPage);
    expect(list.length).toBeGreaterThan(0);
    for (const item of list)
      for (const term of ROADMAP_TERMS) expect(item).not.toMatch(term);
  });
  it("on the homepage", async () => {
    const list = await featureListStrings(LandingPage as () => Promise<React.ReactElement>);
    for (const item of list)
      for (const term of ROADMAP_TERMS) expect(item).not.toMatch(term);
  });
});
```

- [ ] **Step 6: Run the full suite + typecheck**

Run: `cd frontend && npm test && npx tsc --noEmit`
Expected: all green (including the existing `landing-jsonld-faqpage.test.tsx`, which still expects the homepage's 5+ JSON-LD blocks and 6 FAQ entries — unchanged).

- [ ] **Step 7: Commit + PR**

```bash
git add frontend/components/landing/Hero.tsx frontend/components/landing/EverythingInTheApp.tsx frontend/app/page.tsx frontend/tests/features-no-roadmap-in-featurelist.test.tsx
git commit -m "feat(landing): forecasting-first homepage pass with feature strip"
git push -u origin feat/homepage-forecasting-pass
gh pr create --title "feat(landing): forecasting-first homepage content pass" --body "Sharpens the Hero subcopy to lead forecasting, adds an Everything in the app strip linking /features and /compare, and adds shipped-only featureList JSON-LD with a guard test that roadmap items never leak in."
```

---

# PR 5 — Publish PocketSmith and Monarch (staggered, ~2-4 weeks later)

Branch: `feat/publish-pocketsmith-monarch`. Run only after spreadsheets + YNAB are indexed (check Google Search Console).

## Task 12: Flip the two deferred pages live

**Files:**
- Modify: `frontend/app/vs/pocketsmith/page.tsx`, `frontend/app/vs/monarch/page.tsx` (robots)
- Modify: `frontend/app/sitemap.ts`, `frontend/scripts/build-apex.sh` (heredoc), `frontend/public/llms.txt`, `frontend/app/compare/page.tsx` (links), `frontend/tests/seo-public-routes-indexable.test.tsx`

- [ ] **Step 1: Flip both pages to indexable**

In both `page.tsx` files change `robots: { index: false, follow: false }` to `robots: { index: true, follow: true }` and remove the "noindex until launch" comment.

- [ ] **Step 2: Move both metadata entries from the noindex test group to the indexable group**

In `seo-public-routes-indexable.test.tsx`, move `vsPocketsmithMetadata` and `vsMonarchMetadata` from the "staggered ... stay out" `it.each` into the "published /vs pages are indexable" `it.each`. Delete the now-empty staggered describe block.

- [ ] **Step 3: Add both to `app/sitemap.ts`**

```ts
    { url: `${siteUrl}/vs/pocketsmith`, lastModified, changeFrequency: "monthly", priority: 0.7 },
    { url: `${siteUrl}/vs/monarch`, lastModified, changeFrequency: "monthly", priority: 0.7 },
```

- [ ] **Step 4: Add both to the apex sitemap heredoc in `build-apex.sh`**

```bash
  <url><loc>${APEX_URL}/vs/pocketsmith/</loc><lastmod>${BUILD_TIME%T*}</lastmod></url>
  <url><loc>${APEX_URL}/vs/monarch/</loc><lastmod>${BUILD_TIME%T*}</lastmod></url>
```

- [ ] **Step 5: Add both to `public/llms.txt` and link them from `/compare`**

llms.txt under `## Pages`:
```
- [vs PocketSmith](https://thebetterdecision.com/vs/pocketsmith/): Cash-flow forecasting in a simpler, household-shared app, and where PocketSmith goes deeper.
- [vs Monarch](https://thebetterdecision.com/vs/monarch/): Forecasting-first planning with no bank linking required, and where Monarch's aggregation wins.
```
In `app/compare/page.tsx`, extend the "Read the detailed comparisons" line to also link `/vs/pocketsmith` and `/vs/monarch`.

- [ ] **Step 6: Run the full suite, typecheck, apex build**

Run: `cd frontend && npm test && npx tsc --noEmit && npm run build:apex`
Expected: all green; all four `/vs` pages now indexable and in the apex sitemap.

- [ ] **Step 7: Commit + PR**

```bash
git add frontend/app/vs frontend/app/sitemap.ts frontend/scripts/build-apex.sh frontend/public/llms.txt frontend/app/compare/page.tsx frontend/tests/seo-public-routes-indexable.test.tsx
git commit -m "feat(landing): publish PocketSmith and Monarch comparisons"
git push -u origin feat/publish-pocketsmith-monarch
gh pr create --title "feat(landing): publish PocketSmith and Monarch comparisons" --body "Staggered launch step: flips /vs/pocketsmith and /vs/monarch to indexable, adds them to both sitemaps, llms.txt, and the /compare hub links."
```

---

# Side deliverable — distribution copy (not in the build)

## Task 13: Write the distribution copy doc

**Files:**
- Create: `specs/2026-06-09-distribution-copy.md`

- [ ] **Step 1: Write the doc** with these sections (real copy, no em-dashes):

1. **Realistic expectations** — social/HN drives the first wave in days; the comparison pages compound over a quarter; the domain is new so ranking takes weeks.
2. **Show HN** — a title and a body draft. Title: `Show HN: The Better Decision, a privacy-first budgeting and cash-flow forecasting app`. Body leads with the architecture/EU-self-hosted-data-plane and BYO/local AI story (the HN-resonant angle), links the live app, invites critique. Note: post only when the app is solid; one shot.
3. **Reddit "helpful founder" comment template** — a reusable reply that leads with genuine help on the person's problem, then a soft one-line mention. Include the rule note: r/personalfinance and r/Budget auto-remove promotion; r/eupersonalfinance and smaller subs are more tolerant; participate first, link rarely.
4. **Subreddit shortlist** with the tolerance note per sub.

- [ ] **Step 2: Commit (no push needed; it is a working doc)**

```bash
git add specs/2026-06-09-distribution-copy.md
git commit -m "docs(specs): distribution copy for launch"
```

> This commit goes on whichever branch is active; it is not part of a site PR. Hand the file contents back to the user directly as well, since `specs/` is a working area.

---

## Self-Review (completed during authoring)

- **Spec coverage:** /features (T5), /compare (T4), 4x /vs (T7-T10), staggered noindex publish (T9/T10/T12), comparison data module (T1), ComparisonTable (T2), VsPageLayout (T3), AI shipped group + roadmap block (T5), matrix `privacyFirstAi` dimension (T1), homepage pass + featureList shipped-only (T11), all SEO plumbing both hosts (T6, T12), llms.txt (T6, T12), JSON-LD FAQPage on /vs + BreadcrumbList (T3), no Review/Offer schema (none used), honesty guardrails (data in T1, page copy T7-T10), tests for all (T1,T2,T3,T6,T10,T11), distribution side doc (T13). No gaps.
- **Type consistency:** `Competitor`/`Dimension`/`Cell` defined in T1 used unchanged in T2/T3; `VsPageLayout` prop shape in T3 matches its callers in T7-T10; `competitorMeta`/`comparisonMatrix`/`dimensionOrder` names consistent across tasks.
- **Placeholder scan:** no TBD/TODO; the one `.slice(0,2)` shortcut in T4 is flagged with an explicit instruction to replace with the plain array form.
- **Note for executor:** competitor matrix cells (especially `privacyFirstAi` for Monarch and `price`) must be re-verified against live competitor sites before the PR 2/PR 3 reviews; values in T1 are honest and defensible but should be confirmed current.
