"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import SettingsLayout from "@/components/SettingsLayout";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { ApiResponseError, extractErrorMessage } from "@/lib/api";
import { isAdmin } from "@/lib/auth";
import {
  OPENROUTER_KEYS_URL,
  OPENROUTER_PRIVACY_URL,
  completeOpenRouterConnect,
  openRouterStateRef,
  startOpenRouterConnect,
  takeOpenRouterNonce,
} from "@/lib/openrouter-connect";
import {
  btnPrimary,
  btnSecondary,
  card,
  error as errorCls,
  success as successCls,
  warning as warningCls,
} from "@/lib/styles";

// The page OpenRouter redirects to with ?code&state (TBD-632). It finishes the
// connection server-side; the key never reaches the browser.

type NothingSaved =
  | "cancelled"
  | "no_nonce"
  | "busy"
  | "expired"
  | "rate_limited"
  | "unavailable";

type Outcome =
  | { kind: "connecting" }
  | { kind: "success"; label: string | null }
  | { kind: "orphan" | "unknown"; keyName: string }
  | { kind: NothingSaved };

const NOTHING_SAVED: Record<NothingSaved, { title: string; body: string }> = {
  cancelled: {
    title: "Connection cancelled",
    body: "OpenRouter did not approve the connection. Nothing was saved.",
  },
  no_nonce: {
    title: "Start the connection again",
    body: "This page was opened outside the tab that started the connection, so it cannot finish here. Nothing was saved.",
  },
  busy: {
    title: "OpenRouter is busy",
    body: "Nothing was saved. Wait a minute, then connect again.",
  },
  expired: {
    title: "This connection link no longer works",
    body: "It expired, was already used, or was started in another session. Nothing was saved.",
  },
  rate_limited: {
    title: "Too many connection attempts",
    body: "Nothing was saved. Wait an hour, then connect again.",
  },
  unavailable: {
    title: "Something went wrong on our side",
    body: "Nothing was saved. Try again in a moment.",
  },
};

// Refusals the server makes before it calls OpenRouter, so no key exists.
const BEFORE_EXCHANGE: Record<string, NothingSaved> = {
  openrouter_busy: "busy",
  openrouter_code_rejected: "expired",
  oauth_state_invalid: "expired",
  oauth_state_mismatch: "expired",
  oauth_state_used: "expired",
  oauth_rate_limited: "rate_limited",
  oauth_unavailable: "unavailable",
};

function outcomeFor(err: unknown, keyName: string): Outcome {
  // No answer at all: the server may still have finished.
  if (!(err instanceof ApiResponseError)) return { kind: "unknown", keyName };
  const kind = err.code ? BEFORE_EXCHANGE[err.code] : undefined;
  if (kind) return { kind };
  // Auth and body checks run before the handler.
  if ([401, 403, 422].includes(err.status)) return { kind: "expired" };
  return { kind: "orphan", keyName };
}

const linkBtnPrimary = `${btnPrimary} inline-flex items-center`;
const linkBtnSecondary = `${btnSecondary} inline-flex min-h-[44px] items-center`;

export default function OpenRouterCallbackPage() {
  const { user, loading } = useAuth();
  const params = useRef<{ code: string | null; state: string | null } | null>(null);
  const started = useRef(false);
  const [outcome, setOutcome] = useState<Outcome>({ kind: "connecting" });
  const [restarting, setRestarting] = useState(false);
  const [restartError, setRestartError] = useState<string | null>(null);

  // Take code and state off the URL first; the ref survives StrictMode's re-run.
  useEffect(() => {
    if (params.current) return;
    const query = new URLSearchParams(window.location.search);
    params.current = { code: query.get("code"), state: query.get("state") };
    window.history.replaceState(null, "", window.location.pathname);
  }, []);

  useEffect(() => {
    if (loading || !user || !isAdmin(user) || started.current || !params.current) return;
    started.current = true;
    const { code, state } = params.current;
    const nonce = takeOpenRouterNonce();
    if (!code || !state) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- one-shot outcome of reading the callback URL
      setOutcome({ kind: "cancelled" });
      return;
    }
    if (!nonce) {
      setOutcome({ kind: "no_nonce" });
      return;
    }
    const ref = openRouterStateRef(state);
    const keyName = ref ? `The Better Decision (${ref})` : "The Better Decision";
    completeOpenRouterConnect<{ label: string | null }>(code, state, nonce)
      .then((cred) => setOutcome({ kind: "success", label: cred.label }))
      .catch((err) => setOutcome(outcomeFor(err, keyName)));
  }, [loading, user]);

  async function connectAgain() {
    setRestarting(true);
    setRestartError(null);
    try {
      await startOpenRouterConnect();
    } catch (err) {
      setRestartError(extractErrorMessage(err, "Could not open OpenRouter. Try again."));
      setRestarting(false);
    }
  }

  if (user && !isAdmin(user)) {
    return (
      <SettingsLayout activeTab="/settings/ai-providers">
        <p className="text-text-muted">You need admin or owner role to manage AI providers.</p>
      </SettingsLayout>
    );
  }

  const connectAgainButton = (
    <button type="button" className={btnPrimary} onClick={connectAgain} disabled={restarting}>
      {restarting ? "Opening OpenRouter..." : "Connect again"}
    </button>
  );
  const backLink = (primary: boolean) => (
    <Link href="/settings/ai-providers" className={primary ? linkBtnPrimary : linkBtnSecondary}>
      Back to AI providers
    </Link>
  );
  const keysLink = (
    <a href={OPENROUTER_KEYS_URL} target="_blank" rel="noopener noreferrer" className={linkBtnSecondary}>
      Open OpenRouter keys
    </a>
  );

  let title: string;
  let body: React.ReactNode;
  let actions: React.ReactNode = null;
  switch (outcome.kind) {
    case "connecting":
      title = "Connecting OpenRouter";
      body = (
        <div className="mt-3 flex items-center gap-3 text-sm text-text-secondary">
          <Spinner />
          Finishing the connection.
        </div>
      );
      break;
    case "success":
      title = "OpenRouter connected";
      body = (
        <>
          <div className={`${successCls} mt-4`}>
            {outcome.label ? `"${outcome.label}"` : "The new key"} is saved and can be chosen
            in Routing.
          </div>
          <p className="mt-4 text-sm text-text-secondary text-pretty">
            Turn on zero data retention in your OpenRouter privacy settings, so OpenRouter only
            uses providers that do not keep your prompts. The Better Decision leaves this setting
            to you.
          </p>
        </>
      );
      actions = (
        <>
          {backLink(true)}
          <a
            href={OPENROUTER_PRIVACY_URL}
            target="_blank"
            rel="noopener noreferrer"
            className={linkBtnSecondary}
          >
            Open OpenRouter privacy settings
          </a>
        </>
      );
      break;
    case "orphan":
      title = "OpenRouter could not be connected";
      body = (
        <div className={`${warningCls} mt-4 text-pretty`}>
          A key named &quot;{outcome.keyName}&quot; may have been created in your OpenRouter
          account. Delete it at openrouter.ai/settings/keys, then connect again.
        </div>
      );
      actions = (
        <>
          {connectAgainButton}
          {keysLink}
        </>
      );
      break;
    case "unknown":
      title = "We could not confirm the connection";
      body = (
        <p className="mt-2 text-sm text-text-secondary text-pretty">
          If OpenRouter now appears in AI providers, it worked. If it does not, delete the key
          named &quot;{outcome.keyName}&quot; at openrouter.ai/settings/keys, then connect again.
        </p>
      );
      actions = (
        <>
          {backLink(true)}
          {keysLink}
        </>
      );
      break;
    default:
      title = NOTHING_SAVED[outcome.kind].title;
      body = (
        <p className="mt-2 text-sm text-text-secondary text-pretty">
          {NOTHING_SAVED[outcome.kind].body}
        </p>
      );
      actions = (
        <>
          {connectAgainButton}
          {backLink(false)}
        </>
      );
  }

  return (
    <SettingsLayout activeTab="/settings/ai-providers">
      <section className={`${card} max-w-xl p-6`} aria-live="polite">
        <h2 className="text-lg font-semibold text-text-primary text-balance">{title}</h2>
        {body}
        {actions && <div className="mt-6 flex flex-wrap gap-3">{actions}</div>}
        {restartError && <div className={`${errorCls} mt-4`}>{restartError}</div>}
      </section>
    </SettingsLayout>
  );
}
