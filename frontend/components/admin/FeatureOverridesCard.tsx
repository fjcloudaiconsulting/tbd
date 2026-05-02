"use client";

import { useCallback, useEffect, useState } from "react";

import { apiFetch, extractErrorMessage } from "@/lib/api";
import { FEATURE_LABELS } from "@/lib/feature-catalog";
import { btnPrimary, btnSecondary, card, cardHeader, cardTitle, error as errorCls, input, label } from "@/lib/styles";
import type { FeatureKey, FeatureStateResponse, FeatureStateRow } from "@/lib/types";
import Spinner from "@/components/ui/Spinner";

interface Props {
  orgId: number;
}

export default function FeatureOverridesCard({ orgId }: Props) {
  const [state, setState] = useState<FeatureStateResponse | null>(null);
  const [errorMsg, setErrorMsg] = useState("");
  const [editing, setEditing] = useState<FeatureStateRow | null>(null);

  const load = useCallback(async () => {
    setErrorMsg("");
    try {
      const data = await apiFetch<FeatureStateResponse>(
        `/api/v1/admin/orgs/${orgId}/feature-state`,
      );
      setState(data);
    } catch (err) {
      setErrorMsg(extractErrorMessage(err, "Failed to load feature state"));
    }
  }, [orgId]);

  useEffect(() => { load(); }, [load]);

  async function handleRevoke(key: FeatureKey) {
    try {
      await apiFetch(`/api/v1/admin/orgs/${orgId}/feature-overrides/${key}`, {
        method: "DELETE",
      });
      await load();
    } catch (err) {
      setErrorMsg(extractErrorMessage(err, "Failed to revoke override"));
    }
  }

  if (state === null && !errorMsg) {
    return <div className={card}><Spinner /></div>;
  }

  return (
    <div className={card}>
      <div className={cardHeader}>
        <h2 className={cardTitle}>Feature overrides</h2>
      </div>
      {errorMsg && <div className={`${errorCls} m-4`}>{errorMsg}</div>}
      <div className="divide-y divide-border">
        {state?.features.map((row) => (
          <FeatureRow
            key={row.key}
            row={row}
            onEdit={() => setEditing(row)}
            onRevoke={() => handleRevoke(row.key)}
          />
        ))}
      </div>

      {editing && (
        <FeatureOverrideEditModal
          orgId={orgId}
          row={editing}
          onClose={() => setEditing(null)}
          onSaved={async () => { await load(); setEditing(null); }}
        />
      )}
    </div>
  );
}

function FeatureRow({ row, onEdit, onRevoke }: { row: FeatureStateRow; onEdit: () => void; onRevoke: () => void }) {
  const meta = FEATURE_LABELS[row.key];
  const ovr = row.override;
  return (
    <div className={`p-4 ${ovr?.is_expired ? "opacity-60" : ""}`}>
      <div className="font-medium">{meta.label}</div>
      <div className="mt-1 text-sm text-text-muted">
        Plan default: {row.plan_default ? "✓" : "✗"}{" • "}
        Effective: {row.effective ? "✓" : "✗"}
        {ovr && (
          <> • set by {ovr.set_by_email ?? "unknown"}{ovr.expires_at && ` until ${ovr.expires_at}`}</>
        )}
      </div>
      <div className="mt-2 flex gap-2">
        <button onClick={onEdit} className={btnSecondary}>Edit</button>
        {ovr && <button onClick={onRevoke} className={btnSecondary}>Revoke</button>}
      </div>
    </div>
  );
}

function FeatureOverrideEditModal({
  orgId, row, onClose, onSaved,
}: {
  orgId: number; row: FeatureStateRow; onClose: () => void; onSaved: () => void;
}) {
  const [value, setValue] = useState<boolean>(row.override?.value ?? row.plan_default);
  const [expiresAtLocal, setExpiresAtLocal] = useState<string>(
    row.override?.expires_at ? toLocalInput(row.override.expires_at) : "",
  );
  const [note, setNote] = useState<string>(row.override?.note ?? "");
  const [submitting, setSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState("");

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setErrorMsg("");
    try {
      await apiFetch(`/api/v1/admin/orgs/${orgId}/feature-overrides/${row.key}`, {
        method: "PUT",
        body: JSON.stringify({
          value,
          expires_at: expiresAtLocal ? new Date(expiresAtLocal).toISOString() : null,
          note: note || null,
        }),
      });
      onSaved();
    } catch (err) {
      setErrorMsg(extractErrorMessage(err, "Failed to save override"));
      setSubmitting(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <form onSubmit={handleSubmit} className={`${card} w-full max-w-md p-6`}>
        <h2 className="mb-4 text-lg font-semibold">{FEATURE_LABELS[row.key].label}</h2>
        {errorMsg && <div className={`${errorCls} mb-3`}>{errorMsg}</div>}
        <div className="mb-3">
          <label className={label}>Value</label>
          <select
            value={String(value)}
            onChange={(e) => setValue(e.target.value === "true")}
            className={input}
          >
            <option value="true">Granted</option>
            <option value="false">Denied</option>
          </select>
        </div>
        <div className="mb-3">
          <label className={label}>Expires at (your local time, stored as UTC)</label>
          <input
            type="datetime-local"
            value={expiresAtLocal}
            onChange={(e) => setExpiresAtLocal(e.target.value)}
            className={input}
          />
        </div>
        <div className="mb-4">
          <label className={label}>Note (max 500 chars)</label>
          <textarea
            value={note}
            onChange={(e) => setNote(e.target.value)}
            maxLength={500}
            className={`${input} min-h-[80px]`}
          />
        </div>
        <div className="flex justify-end gap-2">
          <button type="button" onClick={onClose} className={btnSecondary}>Cancel</button>
          <button type="submit" disabled={submitting} className={btnPrimary}>
            {submitting ? "Saving..." : "Save"}
          </button>
        </div>
      </form>
    </div>
  );
}

function toLocalInput(iso: string): string {
  // Convert ISO UTC to "YYYY-MM-DDTHH:mm" for datetime-local input.
  const d = new Date(iso);
  const pad = (n: number) => n.toString().padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
