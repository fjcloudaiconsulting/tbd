"use client";

// What agents changed, for review (TBD-581, spec TBD-558 A1.2 and the
// revert ruling of 2026-10-08). Auto-applied rows are marked. Revert stages
// the inverse as a new preview, which applies only on an explicit click.

import { useCallback, useRef, useState } from "react";
import useSWR from "swr";
import { Undo2, X, Zap } from "lucide-react";

import { ApiResponseError, apiFetch } from "@/lib/api";
import { actionHeadline, changeLabel, changeValue, text } from "@/lib/agent/present";
import type { AgentActionRow, DriftRow, StagedAction } from "@/lib/agent/types";
import { maskMoneyText } from "@/lib/format";
import { useBalancesHidden } from "@/lib/hooks/use-org-currency";
import { useFocusTrap } from "@/lib/hooks/use-focus-trap";
import {
  badgeInfo, badgeNeutral, card, cardHeader, cardTitle, filterChip, filterChipOff,
  filterChipOn,
} from "@/lib/styles";

import { rowBtn } from "./AgentTokenList";
import PreviewCard from "./PreviewCard";

type Filter = "all" | "auto";

const REVERT_ERRORS: Record<string, string> = {
  not_done: "Only an applied change can be reverted.",
  not_write: "This kind of change cannot be reverted.",
  no_inverse: "This change has no undo.",
  tool_retired: "This kind of change is no longer available, so it cannot be reverted.",
  no_change: "Nothing to revert: the values already match what they were before.",
};

function revertError(err: unknown): string {
  if (!(err instanceof ApiResponseError)) return "The revert could not be prepared. Try again.";
  const reason = (err.detail as { reason?: string } | null)?.reason;
  return REVERT_ERRORS[(err.code === "not_revertible" && reason) || err.code || ""]
    ?? "The revert could not be prepared. Try again.";
}

function when(iso: string): string {
  const d = new Date(/Z|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`);
  return d.toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

interface Staged {
  row: AgentActionRow;
  action: StagedAction;
  drift?: DriftRow[];
}

function RevertDialog({
  staged, onClose, onDecided,
}: { staged: Staged; onClose: () => void; onDecided: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const titleRef = useRef<HTMLHeadingElement>(null);
  const decided = useRef(false);
  // The card's current action (a stale swap replaces it) and whether a
  // decision is in flight: closing then must not cancel what is being applied.
  const card = useRef({ actionId: staged.action.action_id, busy: false });
  const onState = useCallback((s: { actionId: string; busy: boolean }) => { card.current = s; }, []);
  useFocusTrap({ active: true, containerRef: ref, initialFocusRef: titleRef });
  // Closing without a decision discards the staged revert, so it does not sit
  // pending (and count against the live-preview ceiling) until it expires.
  const close = () => {
    if (!decided.current && !card.current.busy) {
      void apiFetch(`/api/v1/agent/actions/${encodeURIComponent(card.current.actionId)}/cancel`, { method: "POST" })
        .catch(() => {});
    }
    onClose();
  };
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-scrim p-4">
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-labelledby="revert-title"
        onKeyDown={(e) => e.key === "Escape" && close()}
        className="max-h-[90vh] w-full max-w-[min(36rem,calc(100vw-2rem))] overflow-y-auto rounded-lg border border-border bg-surface p-5 shadow-xl"
      >
        <div className="mb-4 flex items-start justify-between gap-4">
          <h2 id="revert-title" ref={titleRef} tabIndex={-1} className="text-lg font-semibold text-text-primary">
            Revert this change
          </h2>
          <button type="button" onClick={close} className="rounded-md p-1 text-text-secondary hover:text-text-primary"
            aria-label="Close">
            <X aria-hidden className="h-5 w-5" strokeWidth={1.75} />
          </button>
        </div>
        <PreviewCard
          tool={staged.row.tool}
          action={staged.action}
          drift={staged.drift}
          applyLabel="Revert change"
          onState={onState}
          onDecided={() => {
            decided.current = true;
            onDecided(); // runs even if the dialog closed while the decision was in flight
          }}
        />
      </div>
    </div>
  );
}

export default function AgentActivity() {
  useBalancesHidden(); // repaint amounts on Hide balances (TBD-527)
  const [filter, setFilter] = useState<Filter>("all");
  const key = `/api/v1/agent/actions?status=done&limit=20${filter === "auto" ? "&mode=auto" : ""}`;
  const { data, error, mutate } = useSWR<{ items: AgentActionRow[] }>(key, (u: string) => apiFetch(u), {
    revalidateOnFocus: false,
    shouldRetryOnError: false,
  });
  const [staged, setStaged] = useState<Staged | null>(null);
  const [rowNote, setRowNote] = useState<{ id: string; text: string } | null>(null);
  const [preparing, setPreparing] = useState<string | null>(null);

  // No `ai.agent` on the plan: the review list is closed (403), the tokens above are not.
  if (error instanceof ApiResponseError && error.status === 403) return null;

  async function revert(row: AgentActionRow) {
    if (preparing) return;
    setPreparing(row.action_id);
    setRowNote(null);
    try {
      const action = await apiFetch<StagedAction>(
        `/api/v1/agent/actions/${encodeURIComponent(row.action_id)}/revert`, { method: "POST" },
      );
      setStaged({ row, action });
    } catch (err) {
      const d = err instanceof ApiResponseError ? (err.detail as (StagedAction & { drift?: DriftRow[] }) | null) : null;
      if (err instanceof ApiResponseError && err.code === "revert_drift" && d?.action_id) {
        setStaged({ row, action: d, drift: d.drift ?? [] });
      } else {
        setRowNote({ id: row.action_id, text: revertError(err) });
      }
    } finally {
      setPreparing(null);
    }
  }

  const rows = data?.items ?? [];

  return (
    <section className={`${card} mb-6`} aria-labelledby="activity-title">
      <div className={`${cardHeader} flex flex-wrap items-center justify-between gap-3`}>
        <h2 id="activity-title" className={cardTitle}>Agent activity</h2>
        <div className="flex gap-2" role="group" aria-label="Show">
          {(["all", "auto"] as Filter[]).map((f) => (
            <button
              key={f}
              type="button"
              aria-pressed={filter === f}
              onClick={() => setFilter(f)}
              className={`${filterChip} px-3 ${filter === f ? filterChipOn : filterChipOff}`}
            >
              {f === "all" ? "All changes" : "Auto-applied"}
            </button>
          ))}
        </div>
      </div>
      <p className="px-6 pt-4 text-sm text-text-secondary">
        Changes made by the assistant and by your connected agents. Revert stages the opposite change
        for you to review before it applies.
      </p>

      {error && !(error instanceof ApiResponseError && error.status === 403) && (
        <p role="alert" className="px-6 py-4 text-sm text-danger">Agent activity could not be loaded.</p>
      )}
      {!data && !error && <p className="px-6 py-6 text-sm text-text-secondary">Loading…</p>}
      {data && rows.length === 0 && (
        <p className="px-6 py-6 text-sm text-text-secondary">
          {filter === "auto" ? "No auto-applied changes yet." : "No changes yet."}
        </p>
      )}

      <ul className="mt-2 divide-y divide-border-subtle">
        {rows.map((r) => {
          const c = r.preview.changes[0];
          const headline = maskMoneyText(actionHeadline(r.tool, r.preview.summary, r.preview.changes));
          const isRevert = Boolean(r.preview.context?.reverts);
          return (
            <li key={r.action_id} className="flex flex-col gap-2 px-6 py-3 sm:flex-row sm:items-start sm:justify-between">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-medium text-text-primary">{headline}</span>
                  {r.mode === "auto" && (
                    <span className={badgeInfo}>
                      <Zap aria-hidden className="h-3 w-3" strokeWidth={2} />
                      Auto
                    </span>
                  )}
                  {isRevert && <span className={badgeNeutral}>Revert</span>}
                </div>
                {c && (
                  <p className="mt-0.5 text-sm text-text-secondary tabular-nums [overflow-wrap:anywhere]">
                    {changeLabel(c)}: <bdi>{changeValue(c, "before", c, r.preview.context ?? {})}</bdi>
                    <span aria-hidden> → </span><span className="sr-only"> to </span>
                    <bdi className="text-text-primary">{changeValue(c, "after", c, r.preview.context ?? {})}</bdi>
                    {text(r.preview.context?.description) && (
                      <span className="block text-xs"><bdi>{text(r.preview.context?.description)}</bdi></span>
                    )}
                  </p>
                )}
                <p className="mt-0.5 text-xs text-text-secondary">
                  {when(r.decided_at ?? r.created_at)} · {r.channel === "mcp" ? "Connected agent" : "Assistant"}
                </p>
                {rowNote?.id === r.action_id && (
                  <p role="status" className="mt-1 text-sm text-text-primary">{rowNote.text}</p>
                )}
              </div>
              {r.risk === "write" && (
                <button
                  type="button"
                  className={`${rowBtn} shrink-0 gap-1.5 self-start`}
                  aria-label={`Revert: ${headline}`}
                  aria-disabled={preparing !== null}
                  onClick={() => revert(r)}
                >
                  <Undo2 aria-hidden className="h-3.5 w-3.5" strokeWidth={1.75} />
                  {preparing === r.action_id ? "Preparing…" : "Revert"}
                </button>
              )}
            </li>
          );
        })}
      </ul>

      {staged && (
        <RevertDialog
          staged={staged}
          onClose={() => setStaged(null)}
          onDecided={() => void mutate()}
        />
      )}
      <div className="h-2" />
    </section>
  );
}
