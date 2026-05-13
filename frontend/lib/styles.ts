export const input =
  "w-full rounded-md border border-border bg-surface-raised px-3 py-2 text-sm text-text-primary placeholder:text-text-muted focus:border-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30";

export const label =
  "mb-1.5 block text-xs font-semibold uppercase tracking-[0.08em] text-text-muted";

// `min-h-[44px]` is baked in to enforce the WCAG / DESIGN.md touch-target
// floor across every primary button without per-call overrides. Callers
// that intentionally collapse the floor on larger viewports may still
// add `sm:min-h-0` (or `md:min-h-0`) after the token.
export const btnPrimary =
  "min-h-[44px] rounded-md bg-accent px-4 py-2 text-sm font-medium text-accent-text hover:bg-accent-hover disabled:opacity-50";

export const btnSecondary =
  "rounded-md border border-border px-4 py-2 text-sm font-medium text-text-primary hover:bg-surface-raised transition-colors";

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

export const stickyBar =
  "sticky top-0 z-20 -mx-4 sm:-mx-8 border-b border-border bg-surface-raised px-4 sm:px-8";

// Brand foundation (L5.10) constants and copy live in `./brand.ts`. They
// were moved out of this file so the design-token check can keep
// `lib/styles.ts` free of hex literals while brand surfaces (OG image,
// apple-icon, landing hero) continue to use a single locked palette
// that does NOT theme-switch. Import from "@/lib/brand".
