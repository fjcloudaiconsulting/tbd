"use client";

import { btnPrimary, btnSecondary, error as errorCls } from "@/lib/styles";

/**
 * Renders the friendly banner that surfaces when /api/v1/auth/sso-stepup/callback
 * redirects back with `?sso_stepup_error=<code>`. Used on `/settings`
 * (email change flow) and `/settings/security` (first-time password set
 * flow), since the callback routes by the `action` slot the initiate
 * call encoded into state. (`pat_mint` failures land on
 * `/system/api-tokens`, which only strips the param; no banner there.)
 *
 * Pages own:
 *   - the per-code copy map (`copyByCode`), so wording can stay
 *     contextual (email change vs password change).
 *   - the retry handler (`onRetry`) — it must re-initiate the step-up
 *     flow with the same `action`, so a successful retry lands back
 *     here, not on the wrong settings page.
 *   - URL cleanup on dismiss/retry (`onDismiss`), so a page refresh
 *     doesn't reshow the banner.
 */
export interface SsoStepupErrorBannerProps {
  errorCode: string;
  copyByCode: Record<string, string>;
  fallbackCopy: string;
  busy?: boolean;
  onRetry: () => void;
  onDismiss: () => void;
}

export default function SsoStepupErrorBanner({
  errorCode,
  copyByCode,
  fallbackCopy,
  busy = false,
  onRetry,
  onDismiss,
}: SsoStepupErrorBannerProps) {
  return (
    <div
      className={errorCls}
      role="alert"
      data-testid="sso-stepup-error-banner"
    >
      <p>{copyByCode[errorCode] ?? fallbackCopy}</p>
      <div className="mt-3 flex flex-wrap gap-2">
        <button
          type="button"
          onClick={onRetry}
          disabled={busy}
          className={`${btnPrimary} text-xs`}
        >
          {busy ? "Redirecting..." : "Try again with Google"}
        </button>
        <button
          type="button"
          onClick={onDismiss}
          className={`${btnSecondary} text-xs`}
        >
          Dismiss
        </button>
      </div>
    </div>
  );
}
