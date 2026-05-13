"use client";

import { FormEvent, useMemo, useState } from "react";

import SlideInPanel from "@/components/floating/SlideInPanel";
import HelpAnchor from "@/components/HelpAnchor";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import {
  btnPrimary,
  btnSecondary,
  error as errorCls,
  input,
  label as labelCls,
  success as successCls,
} from "@/lib/styles";

/**
 * In-app feedback widget — slide-in panel triggered from the
 * AppShellFooter (logged-in only; the trigger renders nothing for
 * unauthed visitors). Pattern reference: TransferForm in the same
 * SlideInPanel chrome for parity with the quick-add experience.
 *
 * Privacy contract (spec captured 2026-05-08):
 *   - Identity opt-in defaults OFF. The checkbox is unchecked on every
 *     open, including after a successful submit.
 *   - Auto-collected context (URL, user-agent, app version, viewport,
 *     theme) is always sent and disclosed to the user via a collapsed
 *     details panel.
 *   - The backend further strips query strings off the URL before
 *     persisting; this is defense-in-depth, not a substitute.
 */

const MESSAGE_MAX = 5000;

type Category = "bug" | "feature" | "other";

interface AutoContext {
  url: string;
  user_agent: string;
  app_version: string;
  viewport_w: number;
  viewport_h: number;
  theme: string;
}

function collectAutoContext(): AutoContext {
  // Strip query + fragment client-side as belt-and-suspenders for the
  // backend's normalization. A URL like `/login?token=xyz` should
  // never even cross the wire.
  const rawUrl =
    typeof window !== "undefined" ? window.location.href : "";
  let cleanUrl = rawUrl;
  try {
    const u = new URL(rawUrl);
    u.search = "";
    u.hash = "";
    cleanUrl = u.toString();
  } catch {
    // Non-parseable URL — fall back to pathname only if available.
    if (typeof window !== "undefined") {
      cleanUrl = `${window.location.origin}${window.location.pathname}`;
    }
  }

  const theme =
    typeof document !== "undefined"
      ? document.documentElement.getAttribute("data-theme") ?? "default"
      : "default";

  return {
    url: cleanUrl,
    user_agent:
      typeof navigator !== "undefined" ? navigator.userAgent : "",
    app_version: process.env.NEXT_PUBLIC_APP_VERSION ?? "dev",
    viewport_w:
      typeof window !== "undefined" ? window.innerWidth : 0,
    viewport_h:
      typeof window !== "undefined" ? window.innerHeight : 0,
    theme,
  };
}

export interface FeedbackWidgetProps {
  open: boolean;
  onClose: () => void;
}

export default function FeedbackWidget({
  open,
  onClose,
}: FeedbackWidgetProps) {
  const [category, setCategory] = useState<Category>("bug");
  const [message, setMessage] = useState("");
  const [includeIdentity, setIncludeIdentity] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  // Recompute on open so a navigation between mounts is reflected.
  const autoContext = useMemo<AutoContext | null>(
    () => (open ? collectAutoContext() : null),
    [open],
  );

  // Reset transient state every time the panel opens. The privacy
  // default (identity opt-OUT) is re-asserted here so a previous
  // tick-and-submit does not survive the close/reopen boundary.
  //
  // Implemented via the "compare state to a prop and call setState
  // during render" pattern from the React docs (see "Adjusting state
  // when a prop changes" — https://react.dev/reference/react/useState#storing-information-from-previous-renders).
  // Doing this during render (not in an effect) avoids the cascading
  // re-render cycle the React 19 lint rule flags on
  // `react-hooks/set-state-in-effect`.
  const [prevOpen, setPrevOpen] = useState(open);
  if (open !== prevOpen) {
    setPrevOpen(open);
    if (open) {
      setMessage("");
      setIncludeIdentity(false);
      setError(null);
      setSuccess(false);
      setCategory("bug");
    }
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (submitting) return;
    setError(null);
    setSuccess(false);

    if (message.trim().length === 0) {
      setError("Please write a short message before sending.");
      return;
    }

    setSubmitting(true);
    try {
      const ctx = autoContext ?? collectAutoContext();
      await apiFetch("/api/v1/feedback", {
        method: "POST",
        body: JSON.stringify({
          message: message.trim(),
          category,
          include_identity: includeIdentity,
          context: {
            url: ctx.url,
            user_agent: ctx.user_agent,
            app_version: ctx.app_version,
            viewport_w: ctx.viewport_w,
            viewport_h: ctx.viewport_h,
            theme: ctx.theme,
          },
        }),
      });
      setSuccess(true);
      setMessage("");
    } catch (err) {
      setError(extractErrorMessage(err) ?? "We could not send your feedback. Please try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <SlideInPanel
      open={open}
      onClose={onClose}
      title="Send feedback"
      testId="feedback-widget"
    >
      <form
        onSubmit={handleSubmit}
        aria-label="Feedback form"
        className="flex flex-col gap-5"
      >
        {success && (
          <div
            className={successCls}
            role="status"
            aria-live="polite"
            data-testid="feedback-success"
          >
            Thanks, we got it. We read every message.
          </div>
        )}
        {error && (
          <div
            className={errorCls}
            role="alert"
            data-testid="feedback-error"
          >
            {error}
          </div>
        )}

        <fieldset className="flex flex-col gap-2">
          <legend className={labelCls}>
            What kind of feedback?{" "}
            <HelpAnchor
              section="feedback-categories"
              label="Feedback categories"
              className="ml-1 inline-flex"
            />
          </legend>
          <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap">
            {[
              { value: "bug", label: "Bug", hint: "Something is broken" },
              { value: "feature", label: "Feature", hint: "Something is missing" },
              { value: "other", label: "Other", hint: "Anything else" },
            ].map((opt) => (
              <label
                key={opt.value}
                className={`flex flex-1 cursor-pointer items-center gap-2 rounded-md border px-3 py-2 text-sm ${
                  category === opt.value
                    ? "border-accent bg-accent-dim text-text-primary"
                    : "border-border text-text-secondary hover:border-accent/40"
                }`}
              >
                <input
                  type="radio"
                  name="feedback-category"
                  value={opt.value}
                  checked={category === opt.value}
                  onChange={() => setCategory(opt.value as Category)}
                  className="h-4 w-4 accent-accent"
                />
                <span className="flex flex-col">
                  <span className="font-medium">{opt.label}</span>
                  <span className="text-xs text-text-muted">{opt.hint}</span>
                </span>
              </label>
            ))}
          </div>
        </fieldset>

        <div className="flex flex-col gap-1.5">
          <label htmlFor="feedback-message" className={labelCls}>
            Your message
          </label>
          <textarea
            id="feedback-message"
            data-testid="feedback-message"
            value={message}
            onChange={(e) => setMessage(e.target.value.slice(0, MESSAGE_MAX))}
            rows={6}
            maxLength={MESSAGE_MAX}
            placeholder="Tell us what happened, what you expected, or what you wish existed."
            className={`${input} resize-y`}
            required
            aria-describedby="feedback-message-counter"
          />
          <div
            id="feedback-message-counter"
            className="text-right text-xs text-text-muted"
          >
            {message.length} / {MESSAGE_MAX}
          </div>
        </div>

        <label className="flex items-start gap-2 rounded-md border border-border bg-surface-raised px-3 py-3 text-sm">
          <input
            type="checkbox"
            data-testid="feedback-include-identity"
            checked={includeIdentity}
            onChange={(e) => setIncludeIdentity(e.target.checked)}
            className="mt-0.5 h-4 w-4 accent-accent"
          />
          <span className="flex flex-col">
            <span className="font-medium text-text-primary">
              Include my account info so we can follow up
            </span>
            <span className="text-xs text-text-muted">
              Optional. Off by default. We only see who you are if you
              tick this box.
            </span>
          </span>
        </label>

        {autoContext && (
          <details
            className="rounded-md border border-border bg-surface-raised px-3 py-2 text-xs text-text-muted"
            data-testid="feedback-context-details"
          >
            <summary className="cursor-pointer text-text-secondary">
              What we collect to triage this
            </summary>
            <dl className="mt-2 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1">
              <dt>Page</dt>
              <dd className="truncate">{autoContext.url}</dd>
              <dt>App version</dt>
              <dd>{autoContext.app_version}</dd>
              <dt>Viewport</dt>
              <dd>
                {autoContext.viewport_w} x {autoContext.viewport_h}
              </dd>
              <dt>Theme</dt>
              <dd>{autoContext.theme}</dd>
              <dt>Browser</dt>
              <dd className="truncate">{autoContext.user_agent}</dd>
            </dl>
            <p className="mt-2">
              We never include balances, transaction details, account
              names, or any other account data.
            </p>
          </details>
        )}

        <div className="flex flex-row-reverse items-center justify-start gap-2 pt-2">
          <button
            type="submit"
            className={btnPrimary}
            disabled={submitting || message.trim().length === 0}
            data-testid="feedback-submit"
          >
            {submitting ? "Sending..." : "Send feedback"}
          </button>
          <button
            type="button"
            className={btnSecondary}
            onClick={onClose}
            data-testid="feedback-cancel"
          >
            Close
          </button>
        </div>
      </form>
    </SlideInPanel>
  );
}
