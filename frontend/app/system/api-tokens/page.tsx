"use client";

// Superadmin PAT management (spec `specs/2026-07-21-superadmin-api-tokens-design.md`
// §9). Mint (form → step-up → reveal-once), list with expiry/status, per-row
// revoke, and a revoke-all panic button next to the account-security copy.

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import SystemLayout from "@/components/SystemLayout";
import { useAuth } from "@/components/auth/AuthProvider";
import { isSuperadmin } from "@/lib/auth";
import { ApiResponseError, extractErrorMessage } from "@/lib/api";
import {
  mintApiToken,
  revokeAllApiTokens,
  revokeApiToken,
} from "@/lib/api-tokens";
import { useApiTokens } from "@/lib/hooks/use-api-tokens";
import type { ApiToken, MintTokenResponse } from "@/lib/types";
import {
  btnDangerSolid,
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
  pageTitle,
} from "@/lib/styles";

import ConfirmModal from "@/components/ui/ConfirmModal";
import MintTokenForm, {
  type MintFormValues,
} from "@/components/system/api-tokens/MintTokenForm";
import RevealOncePanel from "@/components/system/api-tokens/RevealOncePanel";
import StepUpModal, {
  type StepUpProof,
} from "@/components/system/api-tokens/StepUpModal";
import TokenList from "@/components/system/api-tokens/TokenList";
import UsageHelp from "@/components/system/api-tokens/UsageHelp";

// Discriminated revoke intent so a single shared ConfirmModal instance
// drives both the per-row revoke and the panic button.
type RevokeIntent =
  | { kind: "single"; token: ApiToken }
  | { kind: "all" };

export default function SystemApiTokensPage() {
  const { user, loading } = useAuth();
  const router = useRouter();

  const canManage = !!user && isSuperadmin(user);
  const { data, mutate } = useApiTokens(canManage);

  // Capture "now" once so status/expiry math stays pure across renders; the
  // list reloads via mutate, so a mount-time reference is enough.
  const [nowMs] = useState(() => Date.now());

  // Mint flow state.
  const [pendingMint, setPendingMint] = useState<MintFormValues | null>(null);
  const [minting, setMinting] = useState(false);
  const [stepUpError, setStepUpError] = useState<string | null>(null);
  const [revealResult, setRevealResult] = useState<MintTokenResponse | null>(null);

  // Revoke flow state.
  const [revokeIntent, setRevokeIntent] = useState<RevokeIntent | null>(null);
  const [revoking, setRevoking] = useState(false);

  const [pageError, setPageError] = useState("");

  useEffect(() => {
    if (loading) return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (!canManage) {
      router.replace("/dashboard");
    }
  }, [loading, user, canManage, router]);

  if (loading) {
    return (
      <SystemLayout current="API Tokens">
        <h1 className={pageTitle}>API Tokens</h1>
        <div className="flex justify-center py-12">
          <div className="h-6 w-6 animate-spin rounded-full border-2 border-border border-t-accent" />
        </div>
      </SystemLayout>
    );
  }

  if (!canManage) return null;

  const tokens = data?.items ?? [];

  async function handleStepUpSubmit(proof: StepUpProof) {
    if (!pendingMint) return;
    setMinting(true);
    setStepUpError(null);
    try {
      const result = await mintApiToken({
        name: pendingMint.name,
        scope: pendingMint.scope,
        expires_in_days: pendingMint.expiresInDays,
        ...proof,
      });
      setPendingMint(null);
      setRevealResult(result);
      await mutate();
    } catch (err) {
      // A 401 is a failed step-up proof — keep the modal open so the operator
      // can retry. Anything else is a real error surfaced at page level.
      if (err instanceof ApiResponseError && err.status === 401) {
        setStepUpError(err.message);
      } else {
        setPendingMint(null);
        setPageError(extractErrorMessage(err));
      }
    } finally {
      setMinting(false);
    }
  }

  async function handleRevokeConfirm() {
    if (!revokeIntent) return;
    setRevoking(true);
    setPageError("");
    try {
      if (revokeIntent.kind === "single") {
        await revokeApiToken(revokeIntent.token.id);
      } else {
        await revokeAllApiTokens();
      }
      setRevokeIntent(null);
      await mutate();
    } catch (err) {
      setRevokeIntent(null);
      setPageError(extractErrorMessage(err));
    } finally {
      setRevoking(false);
    }
  }

  const revokeCopy =
    revokeIntent?.kind === "single"
      ? {
          title: "Revoke token",
          message: `Revoke "${revokeIntent.token.name}" (${revokeIntent.token.prefix})? Any automation using it will immediately start getting 401s. This cannot be undone.`,
          confirmLabel: "Revoke token",
        }
      : {
          title: "Revoke all tokens",
          message:
            "Revoke every active token you own. All of your automation and scripts will immediately stop authenticating until you mint new tokens. This cannot be undone.",
          confirmLabel: "Revoke all",
        };

  return (
    <SystemLayout current="API Tokens">
      <h1 className={pageTitle}>API Tokens</h1>
      <p className="mt-1 mb-6 max-w-2xl text-sm text-text-secondary">
        Personal access tokens let automation and scripts call the API with a
        long-lived <code>Bearer</code> credential instead of a short-lived
        session token. A token acts as you across every organization.
      </p>

      {pageError && <p className={`${errorCls} mb-4`}>{pageError}</p>}

      {revealResult && (
        <RevealOncePanel
          result={revealResult}
          onDone={() => setRevealResult(null)}
        />
      )}

      <MintTokenForm
        onSubmit={(values) => {
          setStepUpError(null);
          setPendingMint(values);
        }}
        submitting={minting}
      />

      <div className={`${card} mb-6 w-full`}>
        <div className={cardHeader}>
          <h2 className={cardTitle}>Your tokens</h2>
        </div>
        <TokenList
          tokens={tokens}
          nowMs={nowMs}
          onRevoke={(token) => setRevokeIntent({ kind: "single", token })}
        />
      </div>

      <UsageHelp />

      {/* Account security — the revoke-all panic button sits with the copy
          that explains what does (and does not) kill a token. */}
      <div className={`${card} mb-6`}>
        <div className={cardHeader}>
          <h2 className={cardTitle}>Account security</h2>
        </div>
        <div className="space-y-4 p-6">
          <div
            className="space-y-2 text-sm text-text-secondary"
            data-testid="api-token-security-copy"
          >
            <p>
              Tokens <span className="font-semibold text-text-primary">survive
              a password change</span> and{" "}
              <span className="font-semibold text-text-primary">signing out
              everywhere</span> (global session invalidation). The only things
              that kill a token are revoking it or letting it expire. Reach for
              Revoke here during an incident, not the session controls.
            </p>
            <p>
              Even a read-only token can read every organization&apos;s data, so
              treat any leaked token as a full compromise and revoke it
              immediately.
            </p>
          </div>
          <button
            type="button"
            onClick={() => setRevokeIntent({ kind: "all" })}
            className={`${btnDangerSolid} w-full sm:w-auto`}
            data-testid="revoke-all-button"
            disabled={!tokens.some((t) => t.status === "active")}
          >
            Revoke all tokens
          </button>
        </div>
      </div>

      <StepUpModal
        open={pendingMint !== null}
        passwordRequired={user.password_set}
        mfaRequired={user.mfa_enabled}
        submitting={minting}
        errorMessage={stepUpError}
        onSubmit={handleStepUpSubmit}
        onCancel={() => {
          setPendingMint(null);
          setStepUpError(null);
        }}
      />

      <ConfirmModal
        open={revokeIntent !== null}
        title={revokeCopy.title}
        message={revokeCopy.message}
        confirmLabel={revokeCopy.confirmLabel}
        variant="danger"
        submitting={revoking}
        onConfirm={handleRevokeConfirm}
        onCancel={() => setRevokeIntent(null)}
      />
    </SystemLayout>
  );
}
