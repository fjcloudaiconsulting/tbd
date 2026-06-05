# AI Readiness — PR2 (discoverability & onboarding) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make the three AI features self-explanatory and easier to set up: an in-app docs section, a help tooltip on each AI action, a clearer forecast label, and per-provider "get an API key" links in the AI Providers settings form.

**Architecture:** Pure additive UI/content reusing existing systems — the `HelpTooltip` content-map (`lib/help/tooltips.ts`) with `learnMoreSection` deep-links into the `/docs` sections-array page, plus a small per-provider doc-link map in the AI Providers add-credential modal. No backend changes, no migrations.

**Tech Stack:** Next.js 15 + React 19 + TS. Tests: vitest+RTL (kept minimal — operator is compute-capped).

**Spec:** `specs/ai-readiness-gating-onboarding.md`. Builds on PR1 (#396, merged): the 3 surfaces already render the live action when entitled+configured.

**Branch:** `feat/ai-discoverability-onboarding` (exists, off `main`).

**Test reminder:** `docker compose exec frontend npm test -- <path>`; `docker compose exec frontend npx tsc --noEmit`. Default compose project.

---

## File structure

- `frontend/app/docs/page.tsx` — add an `ai-features` entry to the `sections` array + a `<section>` block.
- `frontend/lib/help/tooltips.ts` — add 3 entries (`ai.forecast`, `ai.categorize`, `ai.budget`) to `HELP_TOOLTIPS`.
- `frontend/components/dashboard/AIForecastRefineToggle.tsx` — rename label + add `<HelpTooltip k="ai.forecast" />`.
- `frontend/app/transactions/page.tsx` — add `<HelpTooltip k="ai.categorize" />` next to the live Suggest-category button (both desktop + mobile sites).
- `frontend/app/budgets/page.tsx` — add `<HelpTooltip k="ai.budget" />` next to the live Suggest-rebalance button.
- `frontend/app/settings/ai-providers/page.tsx` — per-provider "Get an API key" link in `AddCredentialModal`.

---

## Task 1: docs "AI features" section + the 3 tooltip entries

**Files:**
- Modify: `frontend/app/docs/page.tsx`, `frontend/lib/help/tooltips.ts`
- Test: `frontend/tests/lib/ai-tooltips.test.ts` (one tiny unit test)

- [ ] **Step 1: Add the docs section.** In `frontend/app/docs/page.tsx`, add to the `sections` array (after `{ id: "forecast-plans", ... }` or near the other feature rows):

```tsx
{ id: "ai-features", label: "AI features" },
```

Then add a matching block in the content body (follow the exact `<section><h2 id="...">...</h2><p>...</p></section>` styling of the neighboring sections, e.g. the `budgets` block):

```tsx
<section>
  <h2 id="ai-features">AI features</h2>
  <p>
    The Better Decision has three optional AI helpers. They are off by default and
    only run when an org admin has connected an AI provider, because each call uses
    your provider account and its tokens (which cost money).
  </p>
  <h3>Setting up a provider</h3>
  <p>
    An org admin opens Settings, then AI providers, and adds a key for OpenAI,
    Anthropic, Ollama, or an OpenAI-compatible endpoint. Keys are encrypted at rest
    and never shown again after you save them. Until a provider is connected, each AI
    action shows a "Set up AI" prompt instead of running.
  </p>
  <h3>Auto-categorize (Transactions)</h3>
  <p>
    When you edit a transaction, "Suggest category" asks the AI for a category based
    on the description and amount. It only fills the picker; nothing is saved until
    you save the transaction.
  </p>
  <h3>Refine forecast with AI (Dashboard)</h3>
  <p>
    "Refine forecast with AI" layers AI-detected seasonal patterns on top of your
    baseline forecast. You choose how much history and how many categories to analyze
    and see the estimated cost before confirming. The result is a preview you can
    revert; it does not change your saved data.
  </p>
  <h3>Suggest rebalance (Budgets)</h3>
  <p>
    On the current period, "Suggest rebalance" asks the AI to propose budget changes
    across categories based on recent spending. You accept or skip each suggestion;
    nothing changes until you apply it.
  </p>
</section>
```

(Match the surrounding indentation + the wrapper that the other `<section>`s live in. Keep copy em-dash-free.)

- [ ] **Step 2: Add the 3 tooltip entries.** In `frontend/lib/help/tooltips.ts`, add to the `HELP_TOOLTIPS` object (the `HelpTooltipKey` type auto-derives via `keyof typeof`, so no other change needed):

```tsx
  // AI features
  "ai.forecast": {
    content:
      "Layers AI-detected seasonal patterns on your baseline forecast. Uses your connected AI provider (costs tokens). The result is a preview you can revert.",
    learnMoreSection: "ai-features",
    triggerLabel: "What does Refine forecast with AI do?",
  },
  "ai.categorize": {
    content:
      "Suggests a category for this transaction using AI. It only fills the picker, nothing is saved until you save. Uses your connected AI provider.",
    learnMoreSection: "ai-features",
    triggerLabel: "What does Suggest category do?",
  },
  "ai.budget": {
    content:
      "Asks the AI to propose budget changes across categories from recent spending. You accept or skip each one. Uses your connected AI provider.",
    learnMoreSection: "ai-features",
    triggerLabel: "What does Suggest rebalance do?",
  },
```

- [ ] **Step 3: Write the tiny unit test**

```ts
// frontend/tests/lib/ai-tooltips.test.ts
import { describe, it, expect } from "vitest";
import { getHelpTooltip } from "@/lib/help/tooltips";

describe("AI feature tooltips", () => {
  it("resolves the 3 AI tooltip keys and deep-links to the ai-features docs section", () => {
    for (const k of ["ai.forecast", "ai.categorize", "ai.budget"] as const) {
      const entry = getHelpTooltip(k);
      expect(entry.content.length).toBeGreaterThan(0);
      expect(entry.learnMoreSection).toBe("ai-features");
    }
  });
});
```

- [ ] **Step 4: Run + tsc**

Run: `docker compose exec frontend npm test -- tests/lib/ai-tooltips.test.ts`
Run: `docker compose exec frontend npx tsc --noEmit` (must be clean — the tooltip keys must type-check).
Expected: PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/app/docs/page.tsx frontend/lib/help/tooltips.ts frontend/tests/lib/ai-tooltips.test.ts
git commit -m "feat(docs): AI features docs section + help tooltip content"
```

---

## Task 2: wire the tooltips onto the 3 surfaces + rename the forecast label

**Files:**
- Modify: `frontend/components/dashboard/AIForecastRefineToggle.tsx`, `frontend/app/transactions/page.tsx`, `frontend/app/budgets/page.tsx`

`HelpTooltip` is a DEFAULT export: `import HelpTooltip from "@/components/help/HelpTooltip";`. Usage: `<HelpTooltip k="ai.forecast" />` placed next to the action.

- [ ] **Step 1: Forecast toggle.** In `AIForecastRefineToggle.tsx`:
  - Rename the visible label "Apply AI refinement" → "Refine forecast with AI" (the `data-testid="ai-forecast-refine-toggle"` stays; check the existing toggle test does not assert the old text — if it asserts `/Apply AI refinement/`, update that assertion to `/Refine forecast with AI/`).
  - Render `<HelpTooltip k="ai.forecast" />` immediately after the toggle button (inside the same flex row), only in the live (idle) state — not on the CTA path.

- [ ] **Step 2: Transactions.** In `transactions/page.tsx`, at BOTH live `SuggestCategoryButton` sites (desktop ~line 1448, mobile ~line 1751, inside the `configured ? (...)` branch), add `<HelpTooltip k="ai.categorize" />` next to the button. Read the current code first to place it in the same inline container.

- [ ] **Step 3: Budgets.** In `budgets/page.tsx`, next to the live "Suggest rebalance" button (the `budgetAi.configured ? (<button .../>)` branch), add `<HelpTooltip k="ai.budget" />`.

- [ ] **Step 4: Run the affected component tests + tsc**

Run: `docker compose exec frontend npm test -- tests/components/dashboard/ai-forecast-refine-toggle.test.tsx tests/app/budgets-ai-gate.test.tsx`
- If the toggle test asserted the old "Apply AI refinement" label, update it to the new label (this is the one expected test edit).
Run: `docker compose exec frontend npx tsc --noEmit`
Expected: PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/components/dashboard/AIForecastRefineToggle.tsx frontend/app/transactions/page.tsx frontend/app/budgets/page.tsx frontend/tests/components/dashboard/ai-forecast-refine-toggle.test.tsx
git commit -m "feat(ai): help tooltips on the 3 AI actions; clearer forecast label"
```

---

## Task 3: per-provider "Get an API key" links in the AI Providers form

**Files:**
- Modify: `frontend/app/settings/ai-providers/page.tsx` (the `AddCredentialModal`)

The modal has `const [provider, setProvider] = useState<Provider>("openai")` and a provider picker. Add a doc-link map and render a conditional link below the provider picker (or next to the API key field).

- [ ] **Step 1: Add the doc-link map** near the top of the file (with the other module consts like `PROVIDER_LABELS`):

```tsx
const PROVIDER_DOC_LINKS: Partial<Record<Provider, { href: string; label: string }>> = {
  openai: { href: "https://platform.openai.com/api-keys", label: "Get an OpenAI API key" },
  anthropic: { href: "https://console.anthropic.com/settings/keys", label: "Get an Anthropic API key" },
  ollama: { href: "https://ollama.com/download", label: "Set up Ollama locally" },
  openai_compatible: {
    href: "https://platform.openai.com/docs/api-reference",
    label: "Use an OpenAI-compatible endpoint",
  },
};
```

- [ ] **Step 2: Render the conditional link** in `AddCredentialModal`, just under the provider `<select>` (read the exact JSX around the provider picker first). The `provider` state drives it:

```tsx
{PROVIDER_DOC_LINKS[provider] && (
  <a
    href={PROVIDER_DOC_LINKS[provider]!.href}
    target="_blank"
    rel="noopener noreferrer"
    className="text-xs text-text-secondary underline hover:text-text-primary"
  >
    {PROVIDER_DOC_LINKS[provider]!.label}
  </a>
)}
```

(Place it where it reads naturally next to the API key input; match surrounding spacing. `native` intentionally has no link — it's platform-managed and gated off.)

- [ ] **Step 3: Run the settings test (if any) + tsc**

Run: `docker compose exec frontend npm test -- tests/app/settings-ai-providers-page.test.tsx` (if it exists; else skip)
Run: `docker compose exec frontend npx tsc --noEmit`
Expected: PASS / clean.

- [ ] **Step 4: Commit**

```bash
git add frontend/app/settings/ai-providers/page.tsx
git commit -m "feat(settings): per-provider get-an-API-key links in the add-credential form"
```

---

## Task 4: verify + open PR

- [ ] **Step 1:** Full frontend suite + tsc: `docker compose exec frontend npm test` then `docker compose exec frontend npx tsc --noEmit` — green/clean.
- [ ] **Step 2: Manual smoke (local):** with the provider configured, each AI action shows a "?" tooltip whose "Learn more" links to `/docs#ai-features`; the dashboard label reads "Refine forecast with AI"; the AI providers add form shows the right "Get an API key" link per selected provider; `/docs` has an "AI features" section in the TOC.
- [ ] **Step 3: Open PR** titled `feat(ai): discoverability — help tooltips, docs section, provider key links`. Concise body, no test-plan section.

---

## Self-review (coverage vs spec)

- Help tooltip on each AI action → Tasks 1-2. ✓
- `/docs` "AI features" section + deep-links → Task 1. ✓
- Clearer "Apply AI refinement" label → Task 2. ✓
- Per-provider "get an API key" links → Task 3. ✓
- No backend / migrations; minimal tests. ✓
- Out of scope (PR1, shipped): gating, CTA, 412, /ai/status.
