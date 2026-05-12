"use client";

import { useId, type MouseEventHandler } from "react";

/**
 * Provider-compliant Google "Sign in with Google" button.
 *
 * Per Google Identity branding guidelines
 * (https://developers.google.com/identity/gsi/web/guides/display-button),
 * this component:
 *   - uses the official 4-color G mark inline (no recolor, no monochrome),
 *   - uses the locked wordmarks "Sign in with Google" / "Sign up with Google",
 *   - meets a 44px minimum touch target,
 *   - meets contrast in light + dark variants (white surface for light,
 *     #131314 surface for dark, both with Google-spec text colors).
 *
 * The component reads `NEXT_PUBLIC_GOOGLE_SSO_ENABLED`. If the flag is not
 * exactly "true", the button is hidden by default to avoid a broken-redirect
 * experience when ops has not configured Google OAuth in this environment.
 * Pass `showWhenDisabled` to keep it rendered as an aria-disabled affordance,
 * with `disabledReason` surfaced via `aria-describedby`.
 */

export type GoogleSSOButtonMode = "signin" | "signup";

export interface GoogleSSOButtonProps {
  /** Click handler. The component does NOT initiate the OAuth flow itself. */
  onClick?: MouseEventHandler<HTMLButtonElement>;
  /** "signin" renders the locked "Sign in with Google" wordmark (default).
   *  "signup" renders the locked "Sign up with Google" wordmark. */
  mode?: GoogleSSOButtonMode;
  /** While true the button is disabled and announces aria-busy. */
  loading?: boolean;
  /** Force-render the button even when the env flag is not "true". Used to
   *  surface a configuration problem to the user rather than silently hide
   *  the entry point. The button will be visually + aria-disabled. */
  showWhenDisabled?: boolean;
  /** Human-readable reason surfaced via aria-describedby when disabled. */
  disabledReason?: string;
}

function isSSOEnabled(): boolean {
  return process.env.NEXT_PUBLIC_GOOGLE_SSO_ENABLED === "true";
}

function GoogleGMark() {
  // Google's official 4-color G mark. Path data + brand hexes come from
  // Google's published asset; do NOT recolor, distort, or replace.
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 18 18"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
      focusable="false"
    >
      <path
        fill="#4285F4"
        d="M17.64 9.2045c0-.6381-.0573-1.2518-.1636-1.8409H9v3.4814h4.8436c-.2086 1.125-.8427 2.0782-1.7959 2.7164v2.2581h2.9087c1.7018-1.5668 2.6836-3.874 2.6836-6.615z"
      />
      <path
        fill="#34A853"
        d="M9 18c2.43 0 4.4673-.806 5.9564-2.1805l-2.9087-2.258c-.8059.54-1.8368.8595-3.0477.8595-2.344 0-4.3282-1.5831-5.0359-3.7104H.957v2.3318C2.4382 15.9831 5.4818 18 9 18z"
      />
      <path
        fill="#FBBC05"
        d="M3.9641 10.71c-.18-.54-.2823-1.1168-.2823-1.71s.1023-1.17.2823-1.71V4.9582H.957C.3477 6.1731 0 7.5477 0 9c0 1.4523.3477 2.8268.957 4.0418l3.0071-2.3318z"
      />
      <path
        fill="#EA4335"
        d="M9 3.5795c1.3214 0 2.5077.4541 3.4405 1.346l2.5813-2.5814C13.4632.8918 11.4259 0 9 0 5.4818 0 2.4382 2.0168.957 4.9582L3.9641 7.29C4.6718 5.1627 6.6559 3.5795 9 3.5795z"
      />
    </svg>
  );
}

export default function GoogleSSOButton({
  onClick,
  mode = "signin",
  loading = false,
  showWhenDisabled = false,
  disabledReason,
}: GoogleSSOButtonProps) {
  const enabled = isSSOEnabled();
  const helperId = useId();

  if (!enabled && !showWhenDisabled) return null;

  const label = mode === "signup" ? "Sign up with Google" : "Sign in with Google";
  const disabled = loading || !enabled;

  // Surface colors are locked by Google's branding guide and live in
  // globals.css under `.gsi-button` (dark default) +
  // `[data-theme="light"] .gsi-button` (light flip). They do NOT consume
  // product theme tokens because Google's spec does not allow recolor.
  const surface = "gsi-button";

  const base =
    "inline-flex w-full min-h-[44px] items-center justify-center gap-3 " +
    "rounded-md px-4 py-2.5 text-sm font-medium " +
    "transition-colors " +
    "focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 " +
    "focus-visible:ring-offset-2 focus-visible:ring-offset-bg " +
    "disabled:cursor-not-allowed disabled:opacity-60";

  return (
    <>
      <button
        type="button"
        onClick={onClick}
        disabled={disabled}
        aria-label={label}
        aria-busy={loading || undefined}
        aria-disabled={!enabled || undefined}
        aria-describedby={!enabled && disabledReason ? helperId : undefined}
        className={`${base} ${surface}`}
        style={{ fontFamily: "'Roboto', system-ui, -apple-system, sans-serif" }}
      >
        <GoogleGMark />
        <span>{loading ? "Connecting..." : label}</span>
      </button>
      {!enabled && disabledReason ? (
        <p id={helperId} className="mt-2 text-xs text-text-muted text-center">
          {disabledReason}
        </p>
      ) : null}
    </>
  );
}
