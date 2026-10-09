"use client";

// Agent access (TBD-581, spec TBD-558 4.5, A1.2, T7, T12): create, lower and
// revoke agent tokens, review what agents changed, and see plan usage. The path
// is fixed by the backend: the SSO step-up for `agent_token_mint` and the token
// notifications all link here.

import { useEffect, useState } from "react";
import useSWR from "swr";

import SettingsLayout from "@/components/SettingsLayout";
import AgentActivity from "@/components/agent/AgentActivity";
import AgentMintForm, { type AgentMintValues } from "@/components/agent/AgentMintForm";
import AgentTokenList from "@/components/agent/AgentTokenList";
import UsageMeters from "@/components/agent/UsageMeters";
import { useAuth } from "@/components/auth/AuthProvider";
import RevealOncePanel from "@/components/system/api-tokens/RevealOncePanel";
import StepUpModal, { type StepUpProof } from "@/components/system/api-tokens/StepUpModal";
import ConfirmModal from "@/components/ui/ConfirmModal";
import Spinner from "@/components/ui/Spinner";
import { ApiResponseError, apiFetch, extractErrorMessage } from "@/lib/api";
import { SCOPE_INFO, lowerScopes } from "@/lib/agent/present";
import type { AgentScope, AgentToken } from "@/lib/agent/types";
import { useAiStatus } from "@/lib/hooks/use-ai-status";
import type { ListEnvelope, MintTokenResponse } from "@/lib/types";
import { btnSecondary, card, cardHeader, cardTitle, error as errorCls, success as successCls } from "@/lib/styles";

const BASE = "/api/v1/agent/tokens";
const STEPUP_ACTION = "agent_token_mint";
// The mint form survives the Google round trip here; never a secret.
const PENDING_KEY = "tbd.agent-token-mint";

type Intent =
  | { kind: "revoke"; token: AgentToken }
  | { kind: "revoke-all" }
  | { kind: "lower"; token: AgentToken };

function readPending(): AgentMintValues | null {
  try {
    const raw = sessionStorage.getItem(PENDING_KEY);
    sessionStorage.removeItem(PENDING_KEY);
    return raw ? (JSON.parse(raw) as AgentMintValues) : null;
  } catch {
    return null;
  }
}

export default function AgentTokensPage() {
  const { user, loading } = useAuth();
  const ai = useAiStatus();
  const { data, mutate } = useSWR<ListEnvelope<AgentToken>>(user ? BASE : null, (u: string) => apiFetch(u), {
    revalidateOnFocus: false,
  });
  const [nowMs] = useState(() => Date.now());

  const [pending, setPending] = useState<AgentMintValues | null>(null);
  const [ssoToken, setSsoToken] = useState<string | null>(null);
  const [ssoBusy, setSsoBusy] = useState(false);
  const [minting, setMinting] = useState(false);
  const [stepUpError, setStepUpError] = useState<string | null>(null);
  const [revealed, setRevealed] = useState<MintTokenResponse | null>(null);

  const [intent, setIntent] = useState<Intent | null>(null);
  const [lowerTo, setLowerTo] = useState<AgentScope | null>(null);
  const [working, setWorking] = useState(false);
  const [pageError, setPageError] = useState("");
  const [pageNote, setPageNote] = useState("");

  // Back from Google: take the proof off the URL at once, then reopen the
  // step-up with the form the user filled before leaving.
  useEffect(() => {
    const url = new URL(window.location.href);
    const hasToken = url.hash.startsWith("#stepup_token=");
    const failed = url.searchParams.has("sso_stepup_error");
    if (!hasToken && !failed) return;
    const token = hasToken ? url.hash.slice("#stepup_token=".length) : null;
    url.hash = "";
    url.searchParams.delete("sso_stepup_error");
    window.history.replaceState(null, "", url.pathname + url.search);
    const values = readPending();
    /* eslint-disable react-hooks/set-state-in-effect -- one-shot restore of the SSO step-up return on mount */
    if (failed) setPageError("Google could not confirm it's you. Try again.");
    if (token && values) {
      setSsoToken(token);
      setPending(values);
    }
    /* eslint-enable react-hooks/set-state-in-effect */
  }, []);

  if (loading || !user) {
    return (
      <SettingsLayout activeTab="/settings/agent-tokens">
        <Spinner />
      </SettingsLayout>
    );
  }

  const tokens = data?.items ?? [];
  const canMint = Boolean(ai?.agent?.entitled) && ai?.usage?.["mcp.calls"]?.limit !== 0;

  async function verifyWithGoogle() {
    if (!pending) return;
    setSsoBusy(true);
    try {
      sessionStorage.setItem(PENDING_KEY, JSON.stringify(pending));
      const res = await apiFetch<{ redirect_url: string }>("/api/v1/auth/sso-stepup/initiate", {
        method: "POST",
        body: JSON.stringify({ action: STEPUP_ACTION }),
      });
      window.location.href = res.redirect_url;
    } catch (err) {
      setStepUpError(extractErrorMessage(err));
      setSsoBusy(false);
    }
  }

  async function mint(proof: StepUpProof) {
    if (!pending) return;
    setMinting(true);
    setStepUpError(null);
    try {
      const result = await apiFetch<MintTokenResponse>(BASE, {
        method: "POST",
        body: JSON.stringify({
          name: pending.name,
          scope: pending.scope,
          expires_in_days: pending.expiresInDays,
          ...(pending.scope === "agent:auto" ? { acknowledge_auto: pending.acknowledgeAuto } : {}),
          ...proof,
        }),
      });
      setPending(null);
      setSsoToken(null);
      setRevealed(result);
      await mutate();
    } catch (err) {
      if (err instanceof ApiResponseError && err.status === 401) {
        // With MFA on, a mistyped code also 401s and leaves the Google proof
        // unspent; keep it so the user can retry the code.
        if (ssoToken && !user?.mfa_enabled) {
          setStepUpError("That confirmation expired. Verify with Google again.");
          setSsoToken(null);
        } else {
          setStepUpError(ssoToken ? "Verification failed. Check the code and try again." : err.message);
        }
      } else {
        setPending(null);
        setSsoToken(null);
        setPageError(extractErrorMessage(err));
      }
    } finally {
      setMinting(false);
    }
  }

  async function runIntent() {
    if (!intent || working) return;
    setWorking(true);
    setPageError("");
    setPageNote("");
    try {
      if (intent.kind === "revoke") {
        await apiFetch(`${BASE}/${intent.token.id}`, { method: "DELETE" });
        setPageNote(`Revoked ${intent.token.name}.`);
      } else if (intent.kind === "revoke-all") {
        await apiFetch(`${BASE}/revoke-all`, { method: "POST" });
        setPageNote("Revoked all your agent tokens.");
      } else if (lowerTo) {
        await apiFetch(`${BASE}/${intent.token.id}`, { method: "PATCH", body: JSON.stringify({ scope: lowerTo }) });
        setPageNote(`${intent.token.name} now has ${SCOPE_INFO[lowerTo].label} access.`);
      }
      setIntent(null);
      await mutate();
    } catch (err) {
      setIntent(null);
      setPageError(extractErrorMessage(err));
    } finally {
      setWorking(false);
    }
  }

  const copy =
    intent?.kind === "revoke"
      ? { title: "Revoke token", message: `Revoke "${intent.token.name}" (${intent.token.prefix})? The agent using it stops working at once. This cannot be undone.`, confirm: "Revoke token", variant: "danger" as const }
      : intent?.kind === "revoke-all"
        ? { title: "Revoke all agent tokens", message: "Every agent connected with one of your tokens stops working at once. This cannot be undone.", confirm: "Revoke all", variant: "danger" as const }
        : { title: "Lower access", message: intent ? `Choose the new access for "${intent.token.name}". To raise it again later, create a new token.` : "", confirm: "Lower access", variant: "default" as const };

  return (
    <SettingsLayout activeTab="/settings/agent-tokens">
      <div className="mb-6 max-w-3xl space-y-2 text-sm text-text-secondary">
        <p>
          Agent tokens let an AI agent you run, such as Claude Desktop or another MCP client, read
          your organization&apos;s data and propose changes. A token acts as you, with the access you
          pick here.
        </p>
        <p>
          Agents read names, descriptions and notes that anyone in your organization can edit, and
          that text could try to steer them. We mark it as data, not instructions, but your agent
          decides what to do with it. Give agents that can browse the web, send email or run commands
          the lowest access that works.
        </p>
      </div>

      {pageError && <p role="alert" className={`${errorCls} mb-4`}>{pageError}</p>}
      <p role="status" className={pageNote ? `${successCls} mb-4` : "sr-only"}>{pageNote}</p>

      {revealed ? (
        <RevealOncePanel result={revealed} onDone={() => setRevealed(null)} />
      ) : canMint ? (
        <AgentMintForm
          submitting={minting}
          onSubmit={(v) => {
            setStepUpError(null);
            setPending(v);
          }}
        />
      ) : null}

      <section className={`${card} mb-6`} aria-labelledby="tokens-title">
        <div className={`${cardHeader} flex flex-wrap items-center justify-between gap-3`}>
          <h2 id="tokens-title" className={cardTitle}>Your agent tokens</h2>
          {tokens.some((t) => t.status === "active") && (
            <button type="button" onClick={() => setIntent({ kind: "revoke-all" })}
              className={`${btnSecondary} min-h-6 py-1 text-xs hover:text-danger`}>
              Revoke all
            </button>
          )}
        </div>
        <AgentTokenList
          tokens={tokens}
          nowMs={nowMs}
          onRevoke={(token) => setIntent({ kind: "revoke", token })}
          onLower={(token) => {
            setLowerTo(lowerScopes(token.scope).at(-1) ?? null);
            setIntent({ kind: "lower", token });
          }}
        />
      </section>

      <AgentActivity />

      {ai?.usage && (
        <UsageMeters usage={ai.usage} meters={["assistant.turns", "mcp.calls"]} title="Plan usage" />
      )}

      <StepUpModal
        open={pending !== null}
        passwordRequired={user.password_set}
        mfaRequired={user.mfa_enabled}
        submitting={minting}
        errorMessage={stepUpError}
        onSubmit={mint}
        onCancel={() => {
          setPending(null);
          setSsoToken(null);
          setStepUpError(null);
        }}
        sso={{ token: ssoToken, onVerify: verifyWithGoogle, busy: ssoBusy }}
      />

      <ConfirmModal
        open={intent !== null}
        title={copy.title}
        message={copy.message}
        confirmLabel={copy.confirm}
        variant={copy.variant}
        submitting={working}
        confirmDisabled={intent?.kind === "lower" && !lowerTo}
        onConfirm={runIntent}
        onCancel={() => setIntent(null)}
      >
        {intent?.kind === "lower" && (
          <fieldset className="mt-3 space-y-2">
            <legend className="sr-only">New access</legend>
            {lowerScopes(intent.token.scope).map((s) => (
              <label key={s} className="flex min-h-6 cursor-pointer items-start gap-2 text-sm">
                <input type="radio" name="lower-scope" value={s} checked={lowerTo === s}
                  onChange={() => setLowerTo(s)} className="mt-1 accent-accent" />
                <span>
                  <span className="block font-medium text-text-primary">{SCOPE_INFO[s].label}</span>
                  <span className="block text-xs text-text-secondary">{SCOPE_INFO[s].hint}</span>
                </span>
              </label>
            ))}
          </fieldset>
        )}
      </ConfirmModal>
    </SettingsLayout>
  );
}
