"use client";

// Org admins list and revoke their members' agent tokens (TBD-619). The server
// is the guard (require_org_admin); the page only mounts this for admins, and a
// 403 (role changed since load) renders nothing.

import { useState } from "react";
import useSWR, { useSWRConfig } from "swr";

import AgentTokenList from "@/components/agent/AgentTokenList";
import ConfirmModal from "@/components/ui/ConfirmModal";
import { ApiResponseError, apiFetch, extractErrorMessage } from "@/lib/api";
import type { OrgAgentToken } from "@/lib/agent/types";
import type { ListEnvelope } from "@/lib/types";
import { card, cardHeader, cardTitle, error as errorCls, success as successCls } from "@/lib/styles";

const ORG = "/api/v1/agent/tokens/org";
const OWN = "/api/v1/agent/tokens";

export default function OrgAgentTokens() {
  const { data, error, mutate } = useSWR<ListEnvelope<OrgAgentToken>>(ORG, (u: string) => apiFetch(u), {
    revalidateOnFocus: false,
    shouldRetryOnError: false,
  });
  const { mutate: globalMutate } = useSWRConfig();
  const [nowMs] = useState(() => Date.now());
  const [target, setTarget] = useState<OrgAgentToken | null>(null);
  const [working, setWorking] = useState(false);
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");

  if (error instanceof ApiResponseError && error.status === 403) return null;
  if (!data && !error) return null;

  async function revoke() {
    if (!target || working) return;
    setWorking(true);
    setErr("");
    setNote("");
    try {
      await apiFetch(`${ORG}/${target.id}`, { method: "DELETE" });
      setNote(`Revoked ${target.name} owned by ${target.owner_email}.`);
      setTarget(null);
    } catch (e) {
      setTarget(null);
      setErr(extractErrorMessage(e));
    } finally {
      // The org list includes the admin's own tokens: refresh both tables, even after a failure.
      await Promise.all([mutate(), globalMutate(OWN)]);
      setWorking(false);
    }
  }

  return (
    <section className={`${card} mb-6`} aria-labelledby="org-tokens-title">
      <div className={cardHeader}>
        <h2 id="org-tokens-title" className={cardTitle}>Organization agent tokens</h2>
        <p className="mt-1 text-xs text-text-secondary">
          Every agent token your members created. Revoke one to cut off its agent at once.
        </p>
      </div>
      {err && <p role="alert" className={`${errorCls} m-4`}>{err}</p>}
      <p role="status" className={note ? `${successCls} m-4` : "sr-only"}>{note}</p>
      {error && !data ? (
        <p role="alert" className={`${errorCls} m-4`}>{extractErrorMessage(error)}</p>
      ) : (
      <AgentTokenList
        tokens={data?.items ?? []}
        nowMs={nowMs}
        showOwner
        emptyText="No one in your organization has created an agent token yet."
        onRevoke={(t) => setTarget(t as OrgAgentToken)}
      />
      )}
      <ConfirmModal
        open={target !== null}
        title="Revoke member's token"
        message={target ? `Revoke "${target.name}" (${target.prefix}) owned by ${target.owner_email}? Their agent stops working at once. This cannot be undone.` : ""}
        confirmLabel="Revoke token"
        variant="danger"
        submitting={working}
        onConfirm={revoke}
        onCancel={() => setTarget(null)}
      />
    </section>
  );
}
