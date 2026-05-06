---
name: The Better Decision
description: Personal finance planning app — line-item budget, forecast, and actuals for individuals and households.
colors:
  bg: "#070d18"
  surface: "#0B1F3A"
  surface-raised: "#122a4a"
  surface-overlay: "#163157"
  border: "#1a3560"
  border-subtle: "#122a4a"
  text-primary: "#E6EAF0"
  text-secondary: "#9ba8bd"
  text-muted: "#5a6a82"
  accent: "#D4A64A"
  accent-hover: "#B88A2E"
  accent-dim: "#D4A64A1F"
  accent-text: "#0B1F3A"
  danger: "#f87171"
  danger-dim: "#F871711F"
  success: "#4ade80"
  success-dim: "#4ADE801F"
  info: "#5FA8D3"
  info-dim: "#5FA8D31F"
  sidebar-bg: "#06101e"
  sidebar-text: "#7a8da6"
  sidebar-text-bright: "#E6EAF0"
  sidebar-active-text: "#D4A64A"
typography:
  display:
    fontFamily: "Fraunces, Georgia, serif"
    fontSize: "1.5rem"
    fontWeight: 500
    lineHeight: 1.15
    letterSpacing: "normal"
  body:
    fontFamily: "Outfit, system-ui, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "normal"
  label:
    fontFamily: "Outfit, system-ui, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 600
    lineHeight: 1
    letterSpacing: "0.08em"
rounded:
  sm: "2px"
  md: "8px"
  lg: "12px"
  full: "9999px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.accent-text}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "8px 16px"
  button-primary-hover:
    backgroundColor: "{colors.accent-hover}"
    textColor: "{colors.accent-text}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "8px 16px"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text-primary}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "8px 16px"
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.lg}"
    padding: "16px 24px"
  input:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.text-primary}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "8px 12px"
  label:
    textColor: "{colors.text-muted}"
    typography: "{typography.label}"
---

# Design System: The Better Decision

## 1. Overview

**Creative North Star: "The Household Ledger"**

A premium financial ledger reimagined for the screen. The system descends from the user's monthly Google Sheet — line-item budgets, per-account separation, executed-vs-forecast totals — but rejects every spreadsheet visual cue (gridlines, uniform rows, no hierarchy). The aesthetic is editorial: a serif display face, a calm dark-navy field, and a single brass accent that earns its place. It is meant to feel like a kept book, not a control panel.

Two registers must coexist without friction: a solo user planning ahead at 11pm on a Tuesday, and a couple reviewing the month over a Saturday coffee. Neither should feel patronized by friendly illustration, nor cornered by corporate stiffness. Numbers, dates, and statuses are the heroes; chrome serves them.

What this system explicitly rejects: the bank-app idiom (heavy navy-and-white corporate chrome, paternalistic copy, big circular avatar greetings, "Dear Customer" tone) and the spreadsheet skin (gridlines as hierarchy, identical rows, no weight contrast, no grouping). The Better Decision is a planning tool that happens to handle money, not a transactional account viewer.

**Key Characteristics:**

- Editorial-confident, not corporate-formal
- Tonal depth, not shadow-heavy
- Single accent, used rarely
- Numbers-first typography
- Calm at rest, expressive on state
- Dark default, light theme available and equally considered

## 2. Colors

A muted financial palette with one warm accent. Deep navies do the work of containment and hierarchy; brass appears only where the eye should land.

### Primary

- **Brass Tally** (`#D4A64A`): The single warm accent. Reserved for primary CTAs, the active item in a list or sidebar, and the focus ring. In a typical screen it should appear in *one* place, two at most. The hover state shifts to **Aged Brass** (`#B88A2E`) and the dim state to its 12%-alpha tint (`accent-dim`) for subtle emphasis like selection backgrounds.

### Neutral (Dark Theme — Default)

- **Night Navy** (`#070d18`): The deepest layer; the page background and the sidebar context.
- **Ledger Navy** (`#0B1F3A`): Surface color for cards, panels, and modals. The brand's anchor color.
- **Raised Navy** (`#122a4a`): One step up from `Ledger Navy`; for inputs, hovered rows, and stand-out content within a surface.
- **Overlay Navy** (`#163157`): Two steps up; for elements floating above a surface (dropdowns, tooltips, popovers).
- **Hairline** (`#1a3560`): Borders and dividers. Always 1px. Never a colored stripe.
- **Hairline Subtle** (`#122a4a`): Same role, lower contrast. For internal divisions inside an already-bordered surface.
- **Paper White** (`#E6EAF0`): Primary text. Optical contrast against `Ledger Navy`.
- **Mist** (`#9ba8bd`): Secondary text — captions, helper copy, table headers.
- **Fog** (`#5a6a82`): Muted text — placeholders, disabled labels, decorative metadata.

### Neutral (Light Theme)

The light theme inverts the surface roles but keeps the *sidebar always navy* — a deliberate choice. The product chrome stays calm regardless of theme, while the data canvas adapts.

- **Page** (`#f0f2f5`): Light theme page background.
- **Surface White** (`#ffffff`): Cards and panels.
- **Surface Cool** (`#f7f8fa`): Raised surfaces and hovered rows.
- **Surface Pearl** (`#f0f2f5`): Overlays.
- **Hairline Light** (`#dde1e8`) / **Hairline Subtle Light** (`#e8ebf0`): Borders.
- Text ramp: **Ledger Navy** (`#0B1F3A`) → **Slate** (`#3d5070`) → **Stone** (`#8895a8`).
- The accent shifts to **Aged Brass** (`#B88A2E`) for AA contrast on white.

### Status

- **Overdue Coral** (`#f87171` dark / `#dc2626` light): Errors, overdrafts, pending-past-due.
- **Settled Green** (`#4ade80` dark / `#16a34a` light): Successful operations, settled status, positive forecasts.
- **Reference Blue** (`#5FA8D3` dark / `#2d7db3` light): Informational tone, neutral notices, link emphasis when context is non-action.

Each status color has a `-dim` 12%-alpha sibling for backgrounds (alert banners, status badges).

### Named Rules

**The One Brass Rule.** The accent appears at most twice on any screen, and ideally once. Its rarity is the point. If you find yourself reaching for `bg-accent` on a third surface in the same view, the answer is restraint, not a fourth.

**The Sidebar-Always-Navy Rule.** The product chrome (sidebar) stays the brand's deepest navy in both themes. Light theme adapts the data canvas, not the navigation frame. This carries the brand identity across themes without requiring a separate "brand mode."

**The No Off-Token Rule.** Status, accent, danger, success, info, surface, border — every color used in the app must come from the theme tokens in `globals.css`. Raw Tailwind palette colors (`amber-500`, `slate-700`, `gray-*`) are forbidden. The current `btnWarning` style violates this and is the only surviving exception; it should be migrated to a `warning` theme token.

## 3. Typography

**Display Font:** Fraunces (with Georgia, serif fallback). A flared, opsz-aware editorial serif. Used sparingly — page titles, hero copy, the occasional emphatic number.

**Body Font:** Outfit (with system-ui, sans-serif fallback). A modern geometric sans with comfortable open apertures at body size. The default for everything that isn't Display.

**Character:** The pairing reads as a thoughtful financial publication: an editorial serif handles names and headlines; a clean sans handles numbers, statuses, and labels. Together they earn the "ledger" frame without going antique.

### Hierarchy

- **Display 2xl** (`Fraunces 500`, `1.5rem` / `font-display text-2xl`): Page titles. Used once per route. Carries the editorial signal.
- **Display lg–xl** (`Fraunces 500`, `1.125rem`–`1.25rem`): Section headers when a page has narrative weight (landing, settings overview).
- **Body Default** (`Outfit 400`, `0.875rem` / `text-sm`): The workhorse. Tables, forms, controls, descriptions. **220 occurrences** in the codebase make this the system's true voice.
- **Body Compact** (`Outfit 400`, `0.75rem` / `text-xs`): Helpers, metadata, table sub-rows. Common (168 occurrences).
- **Label** (`Outfit 600`, `0.75rem` uppercase, `letter-spacing 0.08em`): Form field labels and card titles. The uppercase + tracking treatment is the system's "this is a category, not content" signal.

### Named Rules

**The Display-Is-A-Deposit Rule.** Fraunces is reserved for page titles and one or two emphasis moments per route. It is a deposit you can spend once or twice; spending it three times over-counts and looks decorative. If a heading isn't earning the serif, it should be a Body or Label class.

**The Number Voice Rule.** Currency, dates, and decimal amounts always use Body (Outfit) — never Display. Numbers in a serif read as branding; in a clean sans they read as data. This is a planning tool; the data wins.

**The Body-Is-Sm Rule.** The default body size is `text-sm` (14px), not `text-base`. Tables and dense data benefit from the tighter scale; long-form content is rare in this product. Don't bump everything up to `text-base` reflexively.

## 4. Elevation

The system is **flat by default with tonal depth**. Hierarchy comes from layering surface variants (`bg` → `surface` → `surface-raised` → `surface-overlay`), not from `box-shadow`. The light theme mirrors this with white → cool → pearl gradations.

Shadows exist in the system but are reserved for **state**: a dropdown opening, a modal lifting, a tooltip detaching from its trigger. A card at rest has zero shadow. In total, the codebase uses fewer than 15 shadow utilities — and that number should not grow.

### Shadow Vocabulary (state only)

- **Dropdown / Popover** (`shadow-lg`): Floating menus that detach from a surface. Default for `<Menu>` and `<Combobox>` portals.
- **Modal** (`shadow-xl` or `shadow-2xl`): Modal dialogs only. Always paired with a backdrop overlay.
- **Toast / Notification** (`shadow-sm`): Lightweight floating notifications.

### Named Rules

**The Tonal Depth Rule.** Depth comes from `surface` / `surface-raised` / `surface-overlay`. Reach for a darker or lighter surface variant before reaching for a shadow. If the relationship between two elements can be expressed by their surface tone, that's the answer.

**The State-Only Shadow Rule.** Shadows mark transient state (a popover opened, a modal lifted, a row being dragged). They do not mark permanent hierarchy. A card with `shadow-md` at rest is wrong; the same card with no shadow at rest and `shadow-lg` while being dragged is correct.

## 5. Components

The component primitives live in `frontend/lib/styles.ts` as exported Tailwind utility strings (`btnPrimary`, `btnSecondary`, `card`, `input`, `label`, …). They're imported and composed at the use site rather than wrapped as React components, which keeps each call site visible and explicit.

### Buttons

- **Shape:** `rounded-md` (8px radius). Full-width on mobile, content-width on desktop.
- **Primary** (`btnPrimary`): Brass Tally (`#D4A64A`) background, Ledger Navy (`#0B1F3A`) text, `text-sm font-medium`, padding `8px 16px`. Hover shifts to Aged Brass (`#B88A2E`). Disabled = 50% opacity. The primary action on any view; one per primary region.
- **Secondary** (`btnSecondary`): Transparent background, Hairline (`#1a3560`) border, Paper White (`#E6EAF0`) text, same shape and padding as primary. Hover lightens the background to Raised Navy (`#122a4a`). The cancel/escape pair to a primary.
- **Link / Ghost** (`btnLink`, `btnDanger`): Text-only treatments at `text-xs`, used for inline actions in dense layouts (table rows, footer affordances).
- **Warning** (`btnWarning`): Currently uses raw Tailwind `amber-500` and is the only surviving violation of *The No Off-Token Rule* — migrate to a `warning` theme token in a follow-up.
- **Hit-target rule:** Buttons inside dialogs and forms add `min-h-[44px]` for touch targets. Apply this anywhere a button is the primary affordance and the parent is touch-likely.

### Cards

- **Shape:** `rounded-lg` (12px radius). Larger radius than buttons; cards are containers, buttons are actions.
- **Background:** Ledger Navy (`#0B1F3A`) on dark, white on light.
- **Border:** Hairline (`#1a3560`) on dark, Hairline Light (`#dde1e8`) on light. 1px, always full perimeter. Never a colored side-stripe.
- **Header:** `cardHeader` — `border-b` plus internal padding `px-6 py-4`.
- **Title:** `cardTitle` — uppercase, `text-xs`, `tracking-wider`, Fog (`#5a6a82`). The system's signal that "this is a metadata label, the content below is the value."
- **Shadow:** None at rest. See *The State-Only Shadow Rule.*

### Inputs

- **Shape:** `rounded-md` (8px), full-width.
- **Background:** Raised Navy (`#122a4a`) — one step lighter than the card it sits in. Inputs feel slightly inset.
- **Border:** Hairline at rest. On focus, the border shifts to Brass Tally (`#D4A64A`) and a 30%-alpha brass ring appears (`focus-visible:ring-2 focus-visible:ring-accent/30`). The accent doubles as the focus indicator.
- **Placeholder:** Fog (`text-text-muted`).
- **Label:** Always paired. Uppercase `Outfit 600`, `text-xs`, `tracking-[0.08em]`, color Fog. Sits 6px above the input (`mb-1.5`).

### Status Banners

- **Error** (`error`): Background = Overdue Coral 12%-alpha (`bg-danger-dim`), text = Overdue Coral, `rounded-md`, padding `px-4 py-3`, `text-sm`.
- **Success** (`success`): Background = Settled Green 12%-alpha, text = Settled Green, otherwise identical shape to Error.
- **Info / Trial Banner**: Reference Blue or Brass Tally on a dim tint, depending on tone (advisory vs urgent).

### Navigation (Sidebar)

- **Background:** Sidebar Navy (`#06101e`) — the deepest navy in the system, even in light theme. The chrome carries the brand.
- **Item rest:** Sidebar Text (`#7a8da6`), no background.
- **Item hover:** Sidebar Hover (Brass Tally 6%-alpha background), text shifts to Sidebar Text Bright (`#E6EAF0`).
- **Item active:** Sidebar Active Background (Brass Tally 12%-alpha), text = Brass Tally. The accent indicates the user's current location.
- **Icon style:** Heroicons outline, 18×18, stroke-width 1.5. Inherits `currentColor` from the active state, so the active item's icon goes brass alongside its label.

### Page Title

- `pageTitle` — `font-display text-2xl text-text-primary mb-8`. The single editorial moment per page. One per route; never two.

### Named Rules

**The Composed-Utility Rule.** Component primitives are exported strings in `lib/styles.ts`, not React components. New primitives go there; ad-hoc Tailwind classes that duplicate an existing primitive are wrong. If you find yourself writing `rounded-md bg-accent px-4 py-2 text-sm font-medium text-accent-text`, you are reinventing `btnPrimary`.

**The Pressable-Surfaces Rule.** Anything pressable (`button`, `a`, role="button") has a visible focus state that uses Brass Tally — either as a border, a ring, or both. Default browser focus rings are forbidden; the brass focus is the visible commitment to AA accessibility.

## 6. Do's and Don'ts

### Do

- **Do** use theme tokens (`bg-surface`, `text-text-primary`, `border-border`, etc.) for every color; raw Tailwind palette utilities (`bg-amber-500`, `text-slate-700`) are forbidden except in pre-existing legacy code targeted for cleanup.
- **Do** lead with Body (Outfit) for any UI element that displays a number, date, or status. Numbers wear sans, not serif.
- **Do** reserve Brass Tally for primary CTAs, focus states, and the active sidebar item. One brass moment per region; two at most.
- **Do** prefer surface-tone changes over shadows for elevation at rest. Reach for `bg-surface-raised` before `shadow-md`.
- **Do** import primitives from `lib/styles.ts` (`btnPrimary`, `btnSecondary`, `card`, `input`, `label`). New primitives are added to that file, not reinvented inline.
- **Do** include `min-h-[44px]` on any pressable affordance whose parent is touch-likely (forms, dialogs, primary CTAs in mobile views).
- **Do** keep the sidebar navy in both themes. The product chrome carries the brand; only the data canvas adapts.
- **Do** pair color-coded status with a label, icon, or shape — never color alone. Required for the WCAG 2.2 AA commitment in PRODUCT.md.

### Don't

- **Don't** look like a bank app. No heavy navy-and-white corporate chrome on the data surfaces, no paternalistic "Dear Customer" copy, no big circular avatar greeting on the dashboard. The Better Decision is a planning tool that happens to handle money. (Carries forward from PRODUCT.md anti-references.)
- **Don't** look like a spreadsheet skin. Hierarchy comes from typography, color, spacing, and grouping — not gridlines and uniform rows. If a screen reads like Google Sheets in a wrapper, redesign before shipping. (Carries forward from PRODUCT.md anti-references.)
- **Don't** use `border-left` or `border-right` greater than 1px as a colored accent stripe on cards, alerts, or list items. Use full borders, background tints, or leading numbers/icons.
- **Don't** wrap text in a gradient (`background-clip: text` with a gradient). Use a single solid color. Emphasis through weight or scale.
- **Don't** put `box-shadow` on a card at rest. Shadows are state, not decoration.
- **Don't** use Fraunces for numbers, currency, or status text. Fraunces is for titles and selective emphasis only.
- **Don't** introduce a new accent. The system has one. If a screen seems to call for a second, the answer is contrast through neutrals, not a second hue.
- **Don't** use raw Tailwind palette colors (`amber-500`, `slate-700`, `gray-200`). The single surviving exception (`btnWarning` → `amber-500`) is a known violation tracked for cleanup.
- **Don't** rely on color alone to convey state. Pair every color with text, icon, or shape — required for AA compliance.
- **Don't** put a shadow on the sidebar to "separate" it from the content. The dark navy already does that work.
