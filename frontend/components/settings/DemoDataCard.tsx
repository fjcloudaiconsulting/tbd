"use client";

/**
 * DemoDataCard — owner-only "Load demo data" affordance for Settings
 * → Organization (L3.3 in-app synthetic reseed).
 *
 * Two semantic paths:
 *  1. Append-when-empty (default, safe). Calls
 *     ``POST /api/v1/users/me/onboarding/seed-demo?empty_org_only=true``.
 *     Server refuses with 409 ``org_has_data`` if the org already has
 *     real transactions; we surface a soft note rather than an error.
 *  2. Replace (escape hatch). Reveals an inline typed-confirm panel
 *     (same pattern as the L3.1 Danger Zone — ConfirmModal does not
 *     support typed inputs). On submit calls
 *     ``POST /api/v1/orgs/data/reset`` to wipe, then
 *     ``POST /api/v1/users/me/onboarding/seed-demo?empty_org_only=false``.
 *
 * Visibility: rendered only when the caller is an org owner — the
 * Replace path triggers a destructive wipe, and the backend mirrors
 * the gate (``/api/v1/orgs/data/reset`` is ``require_org_owner``).
 * The component itself guards too so non-owners don't see the loaded
 * gun even if a future call-site forgets the role gate.
 *
 * Audit trail: the seed endpoint always writes an audit row carrying
 * the ``empty_org_only`` intent flag; the reset endpoint writes its
 * own ``org.data.reset`` row. The Replace path leaves a two-row
 * trail (reset + seed); Append leaves one.
 */
import { useState } from "react";
import { mutate } from "swr";
import { useRouter } from "next/navigation";

import { apiFetch, extractErrorMessage } from "@/lib/api";
import {
  btnPrimary,
  btnSecondary,
  btnDangerSolid,
  card,
  cardHeader,
  cardTitle,
  input,
} from "@/lib/styles";
import type { User } from "@/lib/types";

const REPLACE_CONFIRM_PHRASE = "load demo data";

interface Props {
  user: User;
}

export default function DemoDataCard({ user }: Props) {
  const router = useRouter();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  const [replaceOpen, setReplaceOpen] = useState(false);
  const [confirmText, setConfirmText] = useState("");
  const [confirmAck, setConfirmAck] = useState(false);

  // Belt-and-suspenders: the parent page already gates by role, but
  // a future call-site might forget. Hide the destructive surface
  // for non-owners so this component is safe to drop anywhere.
  if (user.role !== "owner") {
    return null;
  }

  function clearMessages() {
    setError(null);
    setInfo(null);
  }

  async function handleAppend() {
    clearMessages();
    setLoading(true);
    try {
      const res = await apiFetch<{
        accounts_created: number;
        transactions_created: number;
      }>("/api/v1/users/me/onboarding/seed-demo?empty_org_only=true", {
        method: "POST",
      });
      setInfo(
        `Loaded demo data: ${res.accounts_created} accounts, ${res.transactions_created} transactions.`,
      );
      // Wipe SWR caches so /accounts, /transactions, /budgets etc.
      // refetch the new rows without showing a stale empty state.
      await mutate(() => true, undefined, { revalidate: true });
    } catch (err) {
      const message = extractErrorMessage(err);
      if (message.includes("org_has_data") || message.includes("409")) {
        setInfo(
          'Your org already has data. Use "Replace with demo data" if you want to start over.',
        );
      } else {
        setError(extractErrorMessage(err, "Could not load demo data."));
      }
    } finally {
      setLoading(false);
    }
  }

  function openReplace() {
    clearMessages();
    setConfirmText("");
    setConfirmAck(false);
    setReplaceOpen(true);
  }

  function closeReplace() {
    if (loading) return;
    setReplaceOpen(false);
    setConfirmText("");
    setConfirmAck(false);
  }

  async function handleReplace() {
    if (confirmText.trim().toLowerCase() !== REPLACE_CONFIRM_PHRASE) return;
    if (!confirmAck) return;
    clearMessages();
    setLoading(true);
    try {
      // Step 1: wipe via the L3.1 owner-only endpoint. It uses its
      // own typed-confirm format `RESET <org name>`, so we must
      // construct that here even though the user already typed the
      // demo-data phrase. Same destructive contract as Danger Zone.
      await apiFetch("/api/v1/orgs/data/reset", {
        method: "POST",
        body: JSON.stringify({
          confirm_phrase: `RESET ${user.org_name}`,
        }),
      });
      // Step 2: seed. The empty_org_only=false signals "I just wiped"
      // for the audit row; the server still enforces emptiness.
      const seed = await apiFetch<{
        accounts_created: number;
        transactions_created: number;
      }>("/api/v1/users/me/onboarding/seed-demo?empty_org_only=false", {
        method: "POST",
      });
      // Clear EVERY SWR cache without revalidating; we navigate away
      // immediately and the destination's hooks refetch fresh.
      await mutate(() => true, undefined, { revalidate: false });
      setReplaceOpen(false);
      setConfirmText("");
      setConfirmAck(false);
      setInfo(
        `Replaced your data with demo data: ${seed.accounts_created} accounts, ${seed.transactions_created} transactions.`,
      );
      router.push("/dashboard");
    } catch (err) {
      setError(extractErrorMessage(err, "Could not replace data with demo data."));
    } finally {
      setLoading(false);
    }
  }

  const phraseMatches =
    confirmText.trim().toLowerCase() === REPLACE_CONFIRM_PHRASE;
  const canSubmitReplace = phraseMatches && confirmAck && !loading;

  return (
    <div className={card} data-testid="settings-demo-data-card">
      <div className={cardHeader}>
        <h2 className={cardTitle}>Demo data</h2>
      </div>
      <div className="p-6 space-y-4">
        <p className="text-sm text-text-secondary">
          Load a small set of sample accounts and transactions so you
          can poke around without entering your own data first. Safe
          to delete when you are done.
        </p>
        {info ? (
          <p
            role="status"
            aria-live="polite"
            className="rounded-md border border-border bg-surface-raised px-3 py-2 text-sm text-text-secondary"
            data-testid="settings-demo-info"
          >
            {info}
          </p>
        ) : null}
        {error ? (
          <p role="alert" className="text-sm text-danger" data-testid="settings-demo-error">
            {error}
          </p>
        ) : null}
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
          <button
            type="button"
            onClick={handleAppend}
            disabled={loading}
            className={btnPrimary}
            data-testid="settings-demo-load"
          >
            {loading && !replaceOpen ? "Loading..." : "Load demo data"}
          </button>
          {!replaceOpen ? (
            <button
              type="button"
              onClick={openReplace}
              disabled={loading}
              className={btnSecondary}
              data-testid="settings-demo-replace-open"
            >
              Replace with demo data
            </button>
          ) : null}
        </div>
        <p className="text-xs text-text-muted">
          Loading only works when your org has no transactions yet.
          Replace wipes your current data first, then loads the
          sample set. Both actions are recorded in the audit log.
        </p>

        {replaceOpen ? (
          <div
            className="mt-2 space-y-3 rounded-md border border-danger/40 p-4"
            data-testid="settings-demo-replace-panel"
          >
            <p className="text-sm text-text-primary font-medium">
              Replace your data with demo data?
            </p>
            <p className="text-sm text-text-secondary">
              This wipes every transaction, account, category, budget,
              forecast plan, recurring template, and billing period in
              your org, and then loads the demo dataset in their place.
              The action cannot be undone.
            </p>
            <label
              className="block text-sm text-text-primary"
              htmlFor="demo-replace-input"
            >
              Type{" "}
              <code className="rounded bg-surface-raised px-1 py-0.5 font-mono text-text-primary">
                load demo data
              </code>{" "}
              to confirm
            </label>
            <input
              id="demo-replace-input"
              type="text"
              className={input}
              value={confirmText}
              onChange={(e) => setConfirmText(e.target.value)}
              placeholder={REPLACE_CONFIRM_PHRASE}
              autoComplete="off"
              data-testid="settings-demo-replace-input"
            />
            <label className="flex items-start gap-2 text-sm text-text-secondary">
              <input
                type="checkbox"
                checked={confirmAck}
                onChange={(e) => setConfirmAck(e.target.checked)}
                className="mt-1"
                data-testid="settings-demo-replace-ack"
              />
              <span>
                I understand this permanently deletes my current org data.
              </span>
            </label>
            <div className="flex gap-3">
              <button
                type="button"
                onClick={handleReplace}
                disabled={!canSubmitReplace}
                className={btnDangerSolid}
                data-testid="settings-demo-replace-confirm"
              >
                {loading ? "Replacing..." : "Replace data"}
              </button>
              <button
                type="button"
                onClick={closeReplace}
                disabled={loading}
                className={btnSecondary}
                data-testid="settings-demo-replace-cancel"
              >
                Cancel
              </button>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}

export { REPLACE_CONFIRM_PHRASE };
