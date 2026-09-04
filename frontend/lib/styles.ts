export const input =
  "w-full rounded-md border border-border bg-surface-raised px-3 py-2 text-sm text-text-primary placeholder:text-text-muted focus:border-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30";

export const label =
  "mb-1.5 block text-xs font-semibold uppercase tracking-[0.08em] text-text-muted";

// ─── Focus ───────────────────────────────────────────────────────────
// TBD-319. The visible focus state is supplied GLOBALLY by the
// `:focus-visible` rule in app/globals.css (@layer base). Every link, button,
// input, select and textarea gets a 2px brass outline for free.
//
// ⚠ Do NOT add a focus class to a new call site. That habit is what produced
// 910 untreated elements and two competing idioms, and the convention fence
// will not stop you -- it polices removals, not omissions.
//
// This is the ONE sanctioned way to opt out, and it exists for a PHYSICAL
// constraint rather than a preference: a wrapper whose 2px-outset outline is
// clipped by an `overflow-hidden` ancestor. A negative offset draws the
// outline inside the element's own box, where the clip cannot reach it
// (WCAG 2.4.11, Focus Not Obscured).
//
// ⚠ Never on inline prose -- a negative offset draws the outline through the
// glyphs.
//
// There is deliberately no exported `focusRing`. Blessing a ring would make
// the opt-out fence a rubber stamp: anyone who found the outline
// inconvenient could paste it and stay green. The ~57 inline ring strings
// that predate this baseline are tracked for deletion, not promotion --
// after the baseline lands most of them have nothing left to do.
export const focusInset =
  "focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-focus";

// `min-h-[44px]` is baked in to enforce the WCAG / docs/design/DESIGN.md touch-target
// floor across every primary button without per-call overrides. Callers
// that intentionally collapse the floor on larger viewports may still
// add `sm:min-h-0` (or `md:min-h-0`) after the token.
export const btnPrimary =
  "min-h-[44px] rounded-md bg-accent px-4 py-2 text-sm font-medium text-accent-text hover:bg-accent-hover disabled:opacity-50";

// Secondary button. A transparent outline defined only by the hairline
// `border` token disappears on the light theme (border ≈ page `bg`), so the
// secondary button is a filled chip: a `surface` fill (tonal depth per
// docs/design/DESIGN.md "flat by default" — no resting shadow) + the stronger
// `border-strong` so it reads as actionable on ANY substrate — on a card the
// fill matches but the defined border still frames it. Matches the documented
// `button-secondary` spec. The canvas/toolbar variant below is identical but
// compact.
export const btnSecondary =
  "rounded-md border border-border-strong bg-surface px-4 py-2 text-sm font-medium text-text-primary transition-colors hover:bg-surface-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30";

// Compact secondary button for canvas/editor toolbars (same contrast as
// btnSecondary, smaller padding so several fit a toolbar row).
export const btnCanvas =
  "rounded-md border border-border-strong bg-surface px-3 py-1.5 text-sm text-text-primary transition-colors hover:bg-surface-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30 disabled:cursor-not-allowed disabled:opacity-60";

// Active/engaged variant of btnCanvas — the Customize/Edit "Done" toggle while
// editing. Keeps the surface fill (stays visible) but swaps to the brass
// accent border + text to signal the active edit mode.
export const btnCanvasActive =
  "rounded-md border border-accent bg-surface px-3 py-1.5 text-sm text-accent transition-colors hover:bg-accent/10 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30";

export const btnDanger =
  "text-xs text-text-muted hover:text-danger";

export const btnDangerSolid =
  "rounded-md bg-danger px-4 py-2 text-sm font-medium text-danger-text hover:bg-danger-hover disabled:opacity-50";

export const btnWarning =
  "rounded-md bg-warning px-4 py-2 text-sm font-medium text-warning-text hover:bg-warning-hover disabled:opacity-50";

export const btnLink =
  "text-xs text-text-muted hover:text-accent";

export const card =
  "rounded-lg border border-border bg-surface";

export const cardHeader =
  "border-b border-border px-6 py-4";

export const cardTitle =
  "text-xs font-medium uppercase tracking-wider text-text-muted";

export const error =
  "rounded-md bg-danger-dim px-4 py-3 text-sm text-danger";

// Banner sibling of `error` / `success` for caution states that are not
// failures: the roster page's verdict when nothing is serious but something
// changes which period a transaction lands in. Same construction as `error`,
// on the `warning` token family (docs/design/DESIGN.md "Warning Amber").
export const warning =
  "rounded-md bg-warning-dim px-4 py-3 text-sm text-warning";

export const success =
  "rounded-md bg-success-dim px-4 py-3 text-sm text-success";

export const pageTitle =
  "mb-8 font-display text-2xl text-text-primary";

export const badgeBase =
  "inline-flex items-center gap-1 rounded px-2 py-0.5 text-xs font-medium";

export const badgeWarning =
  `${badgeBase} bg-warning-dim text-warning`;

export const badgeError =
  `${badgeBase} bg-danger-dim text-danger`;

export const badgeInfo =
  `${badgeBase} bg-info-dim text-info`;

export const badgeSuccess =
  `${badgeBase} bg-success-dim text-success`;

export const badgeNeutral =
  `${badgeBase} bg-surface-raised text-text-secondary`;

// Semantic badge tone -> resolved badge class. A single home so any surface
// that classifies a status into a tone (e.g. loanPayoffStatus in lib/loan.ts)
// resolves to the identical badge token, and two surfaces can't drift on which
// colour a tone gets. Tone is generic (not domain-specific), so it lives here
// beside the tokens rather than in a feature module.
export type BadgeTone = "info" | "success" | "neutral" | "warning";

const BADGE_BY_TONE: Record<BadgeTone, string> = {
  info: badgeInfo,
  success: badgeSuccess,
  neutral: badgeNeutral,
  warning: badgeWarning,
};

export function badgeForTone(tone: BadgeTone): string {
  return BADGE_BY_TONE[tone];
}

export const stickyBar =
  "sticky top-0 z-20 -mx-4 sm:-mx-8 border-b border-border bg-surface-raised px-4 sm:px-8";

// Brand foundation (L5.10) constants and copy live in `./brand.ts`. They
// were moved out of this file so the design-token check can keep
// `lib/styles.ts` free of hex literals while brand surfaces (OG image,
// apple-icon, landing hero) continue to use a single locked palette
// that does NOT theme-switch. Import from "@/lib/brand".
