// Canonical brand constants — NOT theme tokens.
//
// These literals describe brand surfaces (landing hero, OG image, email
// header, app-icon) that must hold a single navy/brass identity in
// every theme and every rendering context (server-side OG generator,
// email client, OS-level app icon). They deliberately bypass the
// app's theme tokens because the theme tokens swap on
// `data-theme="light"` and brand surfaces must NOT swap.
//
// See BRAND.md for usage rules ("Brand surface" + "One Brass Rule").
//
// This file is allow-listed in `frontend/scripts/check-design-tokens.sh`
// so the hex literals below do not trip the token-discipline check.
// Do NOT add new hex literals elsewhere — bring them here instead.

// ─── Brand surface palette ───
// Pinned to the navy ground regardless of the visitor's chosen theme,
// because they appear in screenshots, social shares, and email clients
// where theme is not a meaningful concept.
export const BRAND_INK = "#0B1F3A"; // primary brand ground
export const BRAND_INK_DEEP = "#070d18"; // page background under brand surfaces
export const BRAND_INK_RAISED = "#122a4a"; // raised surface on brand ground
export const BRAND_BRASS = "#D4A64A"; // primary accent
export const BRAND_BRASS_HOVER = "#B88A2E"; // accent on hover / pressed
export const BRAND_BRASS_DIM = "rgba(212, 166, 74, 0.12)"; // tinted brass surface
export const BRAND_PARCHMENT = "#E6EAF0"; // primary text on brand ground
export const BRAND_FOG = "#9ba8bd"; // secondary text on brand ground
export const BRAND_SLATE = "#5a6a82"; // muted text / glyph echo

// ─── Brand voice copy constants ───
// Single source of truth for the lockable strings. Downstream teams
// (landing, email templates, SSO, onboarding) should import these
// rather than re-typing the strings.
export const BRAND_NAME = "The Better Decision";
export const BRAND_NAME_SHORT = "TBD";
export const BRAND_TAGLINE = "There's no best decision. Only better ones.";
export const BRAND_DESCRIPTION =
  "A finance app for normal people. Know what you have, what's coming, and where it goes.";
export const BRAND_DOMAIN = "thebetterdecision.com";
export const BRAND_CONTACT_EMAIL = "hello@thebetterdecision.com";

// ─── Tailwind class helpers for the brand surface ───
// Use on the landing hero, OG-image fallback, and any opt-in
// "brand ground" section that must NOT theme-switch. The literal hex
// values are intentional — Tailwind arbitrary values bypass the theme
// token layer, which is exactly what brand surfaces require.
export const brandSurface =
  "bg-[#0B1F3A] text-[#E6EAF0]"; // navy ground, parchment text
export const brandSurfaceMuted =
  "text-[#9ba8bd]"; // secondary copy on brand ground
export const brandAccentText =
  "text-[#D4A64A]"; // brass emphasis on brand ground
