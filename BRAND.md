# Brand kit — The Better Decision

Canonical reference for product name, voice, palette, and logo usage.
Downstream teams (landing, email, SSO, onboarding, header/footer) consume
this file. Internal-only repo/CLI/DB names are out of scope here — see
`~/.claude/projects/-Users-fjorge-src-pfv/memory/project_brand_consolidation.md`.

## Name

| Surface | Use |
|---|---|
| Product name | **The Better Decision** |
| Short form | **TBD** — only inside the product UI or where the full name has already been established on the page |
| Domain | `thebetterdecision.com` (app: `app.thebetterdecision.com`) |
| Contact | `hello@thebetterdecision.com` |
| Possessive | "The Better Decision's account model…" — never "TBD's" |

Do not write "the Better Decision" (lowercase article). The "The" is part
of the name and is capitalized at the start of a sentence; mid-sentence
it is still capitalized.

## Tagline

> **There's no best decision. Only better ones.**

This is the locked tagline. Do not paraphrase. Use the full two-sentence
form on hero, OG image, and the first email a new user receives.

Secondary line ("Personal finance, calmer.") appears on the OG image
footer and may be used as a one-line tagline where the locked tagline is
too long.

## Voice

Honest, human, quiet-confident. Money is hard; we don't pretend it isn't.

**Do**

- Say "know what you have, what's coming, and where it goes."
- Use second person ("your money", "you'll see") sparingly. Default to
  third-person product voice ("The Better Decision shows…").
- Show concrete details. "Three accounts, two budgets, last month" beats
  "comprehensive financial overview".
- Acknowledge uncertainty. "Forecasts are estimates" is on-brand.
- Use commas, periods, and parentheses. Em-dashes are blocked per user
  policy in customer copy.

**Don't**

- "Revolutionize your finances." We don't promise transformation.
- "Effortlessly." We don't promise effortlessness either.
- Emojis in customer copy.
- Fake urgency ("Sign up today!", "Limited time"). Our pricing is honest;
  our copy follows.
- "AI-powered" framing on any surface. The product uses categorization
  heuristics, not an AI persona.
- Em-dashes in customer copy (locked user policy `feedback_no_em_dashes`).

**Examples**

| Off-brand | On-brand |
|---|---|
| Revolutionize your finances with AI-powered insights! | Know what you have, what's coming, and where it goes. |
| Effortless budgeting in seconds. | Set a budget. Watch it land or not. Adjust. |
| Built for the modern saver. | Built for households who already share money. |
| Don't miss out — sign up today! | When you're ready, sign in. We'll be here. |

## Palette

The app theme (`frontend/app/globals.css`) and the brand surface diverge
deliberately: the app re-themes for light/dark; brand surfaces stay on
the navy ground because they appear in screenshots and email clients
where the visitor's theme is unknown.

**App tokens** — use these for product UI. Light/dark aware.

| Token | Dark | Light | Role |
|---|---|---|---|
| `--color-bg` | `#070d18` | `#f0f2f5` | Page background |
| `--color-surface` | `#0B1F3A` | `#ffffff` | Cards, sheets |
| `--color-surface-raised` | `#122a4a` | `#f7f8fa` | Inputs, hover surfaces |
| `--color-text-primary` | `#E6EAF0` | `#0B1F3A` | Body text |
| `--color-text-secondary` | `#9ba8bd` | `#3d5070` | Supporting copy |
| `--color-text-muted` | `#5a6a82` | `#8895a8` | Labels, helper text |
| `--color-accent` | `#D4A64A` | `#B88A2E` | Primary CTAs, focus rings |
| `--color-info` | `#5FA8D3` | `#2d7db3` | Informational chips |

**Brand surface constants** — `frontend/lib/brand.ts`. These do NOT
theme-switch. They live in a dedicated module (separate from
`lib/styles.ts`) so the design-token check can keep the runtime UI
surface free of hex literals while brand surfaces stay locked.

| Constant | Hex | Role |
|---|---|---|
| `BRAND_INK` | `#0B1F3A` | Brand ground |
| `BRAND_INK_DEEP` | `#070d18` | Page under brand ground |
| `BRAND_INK_RAISED` | `#122a4a` | Raised surface on brand ground |
| `BRAND_BRASS` | `#D4A64A` | Primary accent |
| `BRAND_BRASS_HOVER` | `#B88A2E` | Pressed state |
| `BRAND_BRASS_DIM` | `rgba(212,166,74,0.12)` | Tinted brass surface |
| `BRAND_PARCHMENT` | `#E6EAF0` | Primary text on brand ground |
| `BRAND_FOG` | `#9ba8bd` | Secondary text on brand ground |
| `BRAND_SLATE` | `#5a6a82` | Muted text / mark echo |

**The One Brass Rule.** Brass is reserved for emphasis: primary CTA,
focus ring, the lead chevron in the mark, and the second line of the
locked tagline ("Only better ones."). It must never appear in chart
series or product-data colors. Charts use the info/success/neutral
tokens (see `--color-chart-{1..5}` in globals.css).

## Logo

The mark is two stacked chevrons reading as a decision arrow (">"). The
lower chevron is a muted echo; the upper chevron is brass. The visual
reads as "no best, only better": a good choice ahead of another good one.

### Component usage

Import from `@/components/brand/Logo`:

```tsx
import { Logo, Mark, Wordmark } from "@/components/brand/Logo";

// Default lockup, app header / landing nav / email header
<Logo />

// Inside dense chrome (header at sm breakpoint, footer)
<Logo size="sm" />

// On a brass-filled CTA (e.g. an avatar bubble or notification badge)
<Logo tone="inverse" />

// Wordmark only — body copy mentions, prose
<Wordmark />

// Short form for tight spaces (small modal title, tab name)
<Wordmark short />

// Mark only — favicon, OG image, app icon, big standalone hero glyph
<Mark size="lg" />
```

`tone="muted"` collapses the mark to slate-on-slate and the wordmark to
muted text. Use on the footer secondary line and similar places where
the brand would compete with the surrounding content.

### Usage rules

- **Minimum size.** The mark may scale down to 16px (favicon). The
  wordmark may scale down to 14px. Below that, use `<Mark />` alone.
- **Clear space.** Leave at least the height of one chevron tip on all
  sides of the lockup. The component's `gap-2` between mark and
  wordmark is the canonical inter-element spacing — do not override.
- **Color.** Use the component's `tone` prop. Do not recolor the SVG
  inline; the component already routes through theme tokens.
- **Backgrounds.** The lockup must appear on `--color-bg`,
  `--color-surface`, or `BRAND_INK`. It must NOT appear on brass —
  contrast collapses. If you need a brass-grounded surface, use
  `<Logo tone="inverse" />` (renders the mark in navy and the wordmark
  in `accent-text`).
- **Don't** rotate, distort, recolor, add drop shadows, or place the
  mark inside an outline. The component is the only sanctioned form.
- **Don't** put the wordmark in italic, all-caps, or any tracking other
  than the default `tracking-tight`. The "kicker" uppercase form (e.g.
  in the OG image) uses a separate inline SVG, not the wordmark.

## Asset paths

| Path | Format | Purpose |
|---|---|---|
| `frontend/app/icon.svg` | SVG (32px viewport) | Primary favicon. Served at `/icon.svg` |
| `frontend/app/apple-icon.tsx` | Generated PNG (180×180) | iOS home-screen icon. Served at `/apple-icon` |
| `frontend/app/opengraph-image.tsx` | Generated PNG (1200×630) | Social-share card. Served at `/opengraph-image` |
| `frontend/components/brand/Logo.tsx` | React (inline SVG) | In-app lockup component |

The `apple-icon` and `opengraph-image` files are dynamic — Next.js
generates the PNG at build time via `next/og`. No image binaries are
committed; this keeps the asset visually in lockstep with the
`<Mark />` component and avoids drift between source-of-truth SVG and
shipped raster.

## Imports for downstream teams

| Team | Use |
|---|---|
| Landing (L5.1) | Replace inline "The Better Decision" text in `TopNav.tsx` / `LandingFooter.tsx` with `<Logo />` (footer with `tone="muted" size="sm"`). |
| Email templates | Import the brand surface constants (`BRAND_INK`, `BRAND_BRASS`, etc.) from `@/lib/brand` inline; email clients can't load external SVG, so render the chevron mark inline per template. Copy from `Logo.tsx`. |
| SSO branding | Use `BRAND_BRASS` for the Google sign-in border accent. Keep Google's official button per their brand guidelines — the brand here applies only to the surrounding chrome. |
| Header/Footer pass | Replace any "PFV" / "PFV2" remnants with `<Wordmark />`. Audit grep: `grep -ri 'pfv' frontend/app frontend/components`. |
| Onboarding voice | Pull copy seeds from `BRAND_DESCRIPTION` and the Voice section above. |
