export const input =
  "w-full rounded-md border border-border bg-surface-raised px-3 py-2 text-sm text-text-primary placeholder:text-text-muted focus:border-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30";

export const label =
  "mb-1.5 block text-xs font-semibold uppercase tracking-[0.08em] text-text-muted";

export const btnPrimary =
  "rounded-md bg-accent px-4 py-2 text-sm font-medium text-accent-text hover:bg-accent-hover disabled:opacity-50";

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

// ─── Brand foundation (L5.10) ───
// Additive tokens for canonical brand surfaces. Do NOT use these for app
// chrome — they exist to keep marketing surfaces (landing hero, OG image,
// email headers) on a single tightly-controlled visual track that does
// NOT drift with theme changes. See BRAND.md for usage rules.
//
// Brand surfaces are always presented on the navy ground regardless of
// the visitor's chosen theme, because they appear in screenshots, social
// shares, and email clients where theme is not a meaningful concept.
export const BRAND_INK = "#0B1F3A"; // primary brand ground
export const BRAND_INK_DEEP = "#070d18"; // page background under brand surfaces
export const BRAND_INK_RAISED = "#122a4a"; // raised surface on brand ground
export const BRAND_BRASS = "#D4A64A"; // primary accent
export const BRAND_BRASS_HOVER = "#B88A2E"; // accent on hover / pressed
export const BRAND_BRASS_DIM = "rgba(212, 166, 74, 0.12)"; // tinted brass surface
export const BRAND_PARCHMENT = "#E6EAF0"; // primary text on brand ground
export const BRAND_FOG = "#9ba8bd"; // secondary text on brand ground
export const BRAND_SLATE = "#5a6a82"; // muted text / glyph echo

// Brand voice copy constants — single source of truth for the lockable
// strings. Downstream teams (landing, email templates, SSO, onboarding)
// should import these rather than re-typing the strings.
export const BRAND_NAME = "The Better Decision";
export const BRAND_NAME_SHORT = "TBD";
export const BRAND_TAGLINE = "There's no best decision. Only better ones.";
export const BRAND_DESCRIPTION =
  "A finance app for normal people. Know what you have, what's coming, and where it goes.";
export const BRAND_DOMAIN = "thebetterdecision.com";
export const BRAND_CONTACT_EMAIL = "hello@thebetterdecision.com";

// Tailwind class strings for the canonical brand surface. Use on the
// landing hero, OG-image fallback, and any opt-in "brand ground"
// section that must NOT theme-switch.
export const brandSurface =
  "bg-[#0B1F3A] text-[#E6EAF0]"; // navy ground, parchment text
export const brandSurfaceMuted =
  "text-[#9ba8bd]"; // secondary copy on brand ground
export const brandAccentText =
  "text-[#D4A64A]"; // brass emphasis on brand ground
