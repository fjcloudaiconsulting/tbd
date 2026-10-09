"use client";

// A staged agent write, rendered from the server's stored preview (TBD-581,
// spec TBD-558 4.4). Never from model text: the headline, the change rows and
// the warnings are all server fields. Applying or discarding is an explicit
// click; the model has no way to press either.

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { AlertTriangle, ArrowRight, Check } from "lucide-react";

import { ApiResponseError, apiFetch, extractErrorMessage } from "@/lib/api";
import {
  actionHeadline, changeLabel, changeValue, drillDown, text,
} from "@/lib/agent/present";
import type { ActionChange, DriftRow, StagedAction } from "@/lib/agent/types";
import { maskMoneyText } from "@/lib/format";
import { useBalancesHidden } from "@/lib/hooks/use-org-currency";
import {
  badgeError, badgeNeutral, badgeSuccess, btnPrimary, btnSecondary, cardTitle, label,
  error as errorCls, warning as warningCls,
} from "@/lib/styles";

export type Outcome = "done" | "cancelled" | "expired" | "failed" | "decided";

interface Props {
  tool: string;
  action: StagedAction;
  // Revert only: what moved since the original action ran (409 revert_drift).
  drift?: DriftRow[];
  // Move focus to the heading when the card mounts (a fresh preview).
  autoFocus?: boolean;
  // The brass Apply belongs to one card per screen (the One Brass Rule).
  emphasis?: boolean;
  applyLabel?: string;
  onDecided?: (outcome: Outcome, action: StagedAction) => void;
}

// After a stale swap, Apply ignores clicks briefly, so the second click of a
// double click cannot apply a diff nobody has read.
const REARM_MS = 750;

const OUTCOME: Record<Outcome, { label: string; cls: string }> = {
  done: { label: "Applied", cls: badgeSuccess },
  cancelled: { label: "Discarded", cls: badgeNeutral },
  expired: { label: "Expired", cls: badgeNeutral },
  failed: { label: "Not applied", cls: badgeError },
  decided: { label: "Already decided", cls: badgeNeutral },
};

function subject(tool: string, ctx: Record<string, unknown>): string {
  return text(tool === "transactions_set_category" ? ctx.description : ctx.category_name);
}

function timeOf(iso: string): string {
  const d = new Date(/Z|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`);
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

export default function PreviewCard({
  tool, action: initial, drift, autoFocus, emphasis = true, applyLabel = "Apply change", onDecided,
}: Props) {
  useBalancesHidden(); // repaint amounts on Hide balances (TBD-527)
  const [action, setAction] = useState(initial);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [busy, setBusy] = useState<"confirm" | "cancel" | null>(null);
  const [notice, setNotice] = useState("");
  const [failure, setFailure] = useState("");
  const busyRef = useRef(false);
  const armedAt = useRef(0);
  const headingRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    if (autoFocus) headingRef.current?.focus();
  }, [autoFocus]);

  const finish = (o: Outcome, a: StagedAction) => {
    setOutcome(o);
    headingRef.current?.focus(); // the buttons are gone; keep the reader in the card
    onDecided?.(o, a);
  };

  async function decide(kind: "confirm" | "cancel") {
    if (busyRef.current || (kind === "confirm" && Date.now() < armedAt.current)) return;
    busyRef.current = true;
    setBusy(kind);
    setFailure("");
    try {
      await apiFetch(`/api/v1/agent/actions/${encodeURIComponent(action.action_id)}/${kind}`, {
        method: "POST",
      });
      finish(kind === "confirm" ? "done" : "cancelled", action);
    } catch (err) {
      const code = err instanceof ApiResponseError ? err.code : undefined;
      const detail = (err instanceof ApiResponseError ? err.detail : null) as
        | (StagedAction & { status?: string })
        | null;
      if (code === "preview_stale" && detail?.action_id) {
        setAction({ ...detail });
        setNotice("The data changed since this was proposed. Review the updated change.");
        armedAt.current = Date.now() + REARM_MS;
        headingRef.current?.focus();
      } else if (code === "action_expired") {
        finish("expired", action);
      } else if (code === "action_already_decided") {
        const s = detail?.status;
        finish(s === "done" ? "done" : s === "cancelled" ? "cancelled" : "decided", action);
      } else {
        setFailure(extractErrorMessage(err, "The change was not applied."));
        if (err instanceof ApiResponseError && err.status === 403) finish("failed", action);
      }
    } finally {
      busyRef.current = false;
      setBusy(null);
    }
  }

  const ctx = action.context ?? {};
  const primary = action.changes[0];
  const about = subject(tool, ctx);
  const link = drillDown(primary);
  const headingId = `preview-${action.action_id}`;
  const sameRow = (d: DriftRow) => (c: ActionChange) =>
    c.entity === d.entity && text(c.id) === text(d.id) && c.field === d.field;

  return (
    <section
      aria-labelledby={headingId}
      className="rounded-lg border border-border bg-surface"
      data-testid="preview-card"
    >
      <div className="px-5 pt-4">
        <p className={cardTitle}>Proposed change</p>
        <h3
          id={headingId}
          ref={headingRef}
          tabIndex={-1}
          className="mt-1 text-base font-semibold text-text-primary"
        >
          {maskMoneyText(actionHeadline(tool, action.summary, action.changes))}
        </h3>
        {about && (
          <p className="mt-0.5 text-sm text-text-secondary [overflow-wrap:anywhere]">
            <bdi>{about}</bdi>
          </p>
        )}
      </div>

      <dl className="mt-3 divide-y divide-border-subtle border-y border-border-subtle">
        {action.changes.map((c, i) => (
          <div
            key={`${c.entity}-${text(c.id)}-${c.field}-${i}`}
            className="grid gap-1 px-5 py-2.5 sm:grid-cols-[11rem_1fr] sm:gap-4"
          >
            <dt className="text-xs text-text-secondary sm:pt-0.5">{changeLabel(c)}</dt>
            <dd className="flex flex-wrap items-center gap-x-2 text-sm tabular-nums [overflow-wrap:anywhere]">
              <bdi className="text-text-secondary">{changeValue(c, "before", primary, ctx)}</bdi>
              <ArrowRight aria-hidden className="h-3.5 w-3.5 shrink-0 text-text-muted" strokeWidth={1.75} />
              <span className="sr-only">to</span>
              <bdi className="font-medium text-text-primary">{changeValue(c, "after", primary, ctx)}</bdi>
            </dd>
          </div>
        ))}
      </dl>

      <div className="space-y-3 px-5 py-4">
        {action.warnings.map((w) => (
          <p key={w} className={`${warningCls} flex gap-2`}>
            <AlertTriangle aria-hidden className="mt-0.5 h-4 w-4 shrink-0" strokeWidth={1.75} />
            <span>{w}</span>
          </p>
        ))}

        {drift && drift.length > 0 && (
          <div data-testid="drift">
            <p className={`${warningCls} flex gap-2`}>
              <AlertTriangle aria-hidden className="mt-0.5 h-4 w-4 shrink-0" strokeWidth={1.75} />
              <span>This changed after the agent applied it. Reverting overwrites the current value.</span>
            </p>
            <table className="mt-3 w-full text-left text-sm">
              <thead>
                <tr>
                  <th scope="col" className={`${label} pb-1 pr-3`}>Field</th>
                  <th scope="col" className={`${label} pb-1 pr-3`}>After the change</th>
                  <th scope="col" className={`${label} pb-1`}>Now</th>
                </tr>
              </thead>
              <tbody>
                {drift.map((d, i) => {
                  const c: ActionChange = {
                    entity: d.entity, id: d.id, field: d.field, before: d.expected, after: d.current,
                    currency: action.changes.find(sameRow(d))?.currency,
                  };
                  return (
                    <tr key={`${d.field}-${i}`} className="align-top tabular-nums">
                      <td className="py-1 pr-3 text-text-secondary">{changeLabel(c)}</td>
                      <td className="py-1 pr-3 text-text-primary"><bdi>{changeValue(c, "before", primary, ctx)}</bdi></td>
                      <td className="py-1 font-medium text-text-primary"><bdi>{changeValue(c, "after", primary, ctx)}</bdi></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {notice && !outcome && <p role="status" className="text-sm text-info">{notice}</p>}
        {failure && <p role="alert" className={errorCls}>{failure}</p>}

        <div className="flex flex-col-reverse gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-text-secondary">
            {!outcome && <span>Expires at {timeOf(action.expires_at)}</span>}
            {link && (
              <Link
                href={link.href}
                className="inline-flex min-h-6 items-center text-sm text-text-primary underline underline-offset-2 hover:text-accent"
              >
                {link.label}
              </Link>
            )}
          </div>
          {outcome ? (
            <p role="status" className="flex items-center">
              <span className={OUTCOME[outcome].cls}>
                {outcome === "done" && <Check aria-hidden className="h-3.5 w-3.5" strokeWidth={2} />}
                {OUTCOME[outcome].label}
              </span>
            </p>
          ) : (
            <div className="flex flex-col-reverse gap-2 sm:flex-row">
              <button
                type="button"
                onClick={() => decide("cancel")}
                aria-disabled={busy !== null}
                className={`${btnSecondary} min-h-[44px] aria-disabled:opacity-60 sm:min-h-0`}
              >
                {busy === "cancel" ? "Discarding…" : "Discard"}
              </button>
              <button
                type="button"
                onClick={() => decide("confirm")}
                aria-disabled={busy !== null}
                className={`${emphasis ? btnPrimary : btnSecondary} min-h-[44px] aria-disabled:opacity-60 sm:min-h-0`}
              >
                {busy === "confirm" ? "Applying…" : drift?.length ? "Revert anyway" : applyLabel}
              </button>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
