// Canonical brand component for "The Better Decision".
//
// Provides three exports:
//   <Mark />     — square glyph (the chevron stack). Use for favicons,
//                  app-icon contexts, tight headers, OG image, anywhere
//                  the wordmark would not fit.
//   <Wordmark /> — typographic wordmark only. Use inline in copy or
//                  when the mark would compete with surrounding chrome.
//   <Logo />     — the full lockup (mark + wordmark, horizontal). Default
//                  brand presentation; use this in the global top nav,
//                  the landing nav, and email headers.
//
// All three are SVG-inline so they scale crisply at every size and pick
// up the current text/accent color from the surrounding theme — no
// separate light/dark asset is needed.
//
// Design rationale (BRAND.md §Logo): the mark is two stacked chevrons
// reading as a decision arrow. The lower chevron is a muted echo, the
// upper chevron is brass — "no best, only better": a good choice ahead
// of another good one.

import * as React from "react";

type Tone = "default" | "inverse" | "muted";

type Size = "sm" | "md" | "lg";

interface MarkProps extends React.SVGAttributes<SVGSVGElement> {
  /** Visual emphasis. `default` uses brand accent + theme text. `inverse`
   *  flips for use on a brand-accent fill. `muted` collapses both chevrons
   *  to the muted token for footer/secondary placements. */
  tone?: Tone;
  /** Pixel size. `sm` 16, `md` 24, `lg` 40. Pass a custom number via
   *  `width`/`height` on the underlying SVG to override. */
  size?: Size;
  /** Optional accessible label. Pass `null` to render the mark as
   *  decorative (aria-hidden) alongside a wordmark or label. */
  label?: string | null;
}

interface WordmarkProps extends React.HTMLAttributes<HTMLSpanElement> {
  /** Show the short form ("TBD") instead of "The Better Decision". */
  short?: boolean;
  /** Visual emphasis. `default` uses theme primary. `muted` for footer
   *  placements. `inverse` is reserved for accent fills. */
  tone?: Tone;
  /** Optional accessible label override. Defaults to the visible text. */
  label?: string;
}

interface LogoProps extends React.HTMLAttributes<HTMLSpanElement> {
  /** Short form swaps the wordmark for "TBD". */
  short?: boolean;
  /** Visual emphasis. */
  tone?: Tone;
  /** Pixel size for the mark. Wordmark scales with surrounding text. */
  size?: Size;
}

const MARK_PIXELS: Record<Size, number> = {
  sm: 16,
  md: 24,
  lg: 40,
};

const WORDMARK_CLASS: Record<Tone, string> = {
  default: "text-text-primary",
  inverse: "text-accent-text",
  muted: "text-text-muted",
};

/**
 * Stand-alone brand mark. Decorative by default unless `label` is set.
 */
export function Mark({
  tone = "default",
  size = "md",
  label = "The Better Decision",
  className,
  ...rest
}: MarkProps) {
  const px = MARK_PIXELS[size];
  const isDecorative = label === null;
  const titleId = React.useId();

  // tone -> chevron stroke styles. We rely on CSS custom properties so
  // the mark recolors automatically with theme changes.
  const echoColor =
    tone === "inverse"
      ? "var(--color-accent-text)"
      : "var(--color-text-muted)";
  const leadColor =
    tone === "inverse"
      ? "var(--color-accent-text)"
      : tone === "muted"
        ? "var(--color-text-muted)"
        : "var(--color-accent)";
  const echoOpacity = tone === "muted" ? 0.55 : 0.55;
  const leadOpacity = tone === "inverse" ? 0.85 : 1;

  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width={px}
      height={px}
      viewBox="0 0 32 32"
      className={className}
      role={isDecorative ? undefined : "img"}
      aria-hidden={isDecorative ? true : undefined}
      aria-labelledby={isDecorative ? undefined : titleId}
      focusable="false"
      {...rest}
    >
      {!isDecorative && <title id={titleId}>{label}</title>}
      <path
        d="M 9 8 L 18 16 L 9 24"
        fill="none"
        stroke={echoColor}
        strokeWidth={2.5}
        strokeLinecap="round"
        strokeLinejoin="round"
        opacity={echoOpacity}
      />
      <path
        d="M 14 8 L 23 16 L 14 24"
        fill="none"
        stroke={leadColor}
        strokeWidth={2.5}
        strokeLinecap="round"
        strokeLinejoin="round"
        opacity={leadOpacity}
      />
    </svg>
  );
}

/**
 * Typographic wordmark. Renders inline so it inherits the surrounding
 * font size by default. Pass Tailwind size classes via `className` to
 * scale (e.g. `text-lg`, `text-2xl`).
 */
export function Wordmark({
  short = false,
  tone = "default",
  label,
  className = "",
  ...rest
}: WordmarkProps) {
  const text = short ? "TBD" : "The Better Decision";
  const ariaLabel = label ?? text;
  return (
    <span
      className={`font-display font-semibold tracking-tight ${WORDMARK_CLASS[tone]} ${className}`.trim()}
      aria-label={ariaLabel}
      {...rest}
    >
      {text}
    </span>
  );
}

/**
 * Full horizontal lockup: mark + wordmark. Default brand presentation.
 * The mark itself is decorative because the wordmark already carries
 * the accessible name.
 */
export function Logo({
  short = false,
  tone = "default",
  size = "md",
  className = "",
  ...rest
}: LogoProps) {
  return (
    <span
      className={`inline-flex items-center gap-2 ${className}`.trim()}
      {...rest}
    >
      <Mark tone={tone} size={size} label={null} />
      <Wordmark short={short} tone={tone} />
    </span>
  );
}

export default Logo;
