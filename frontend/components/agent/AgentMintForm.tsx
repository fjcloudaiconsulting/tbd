"use client";

// Create an agent token (TBD-581, spec TBD-558 4.5 and A1.2). Auto-apply
// takes an explicit acknowledgment and lives at most 30 days; the server
// refuses either missing (422), and this form never sends one without it.

import { FormEvent, useState } from "react";
import { AlertTriangle } from "lucide-react";

import { SCOPE_INFO } from "@/lib/agent/present";
import type { AgentScope } from "@/lib/agent/types";
import { btnPrimary, card, cardHeader, cardTitle, input, label, warning as warningCls } from "@/lib/styles";

export interface AgentMintValues {
  name: string;
  scope: AgentScope;
  expiresInDays: number;
  acknowledgeAuto: boolean;
}

const EXPIRY = [7, 30, 60, 90] as const;
const AUTO_MAX_DAYS = 30;

interface Props {
  onSubmit: (values: AgentMintValues) => void;
  submitting?: boolean;
  initial?: AgentMintValues | null;
}

export default function AgentMintForm({ onSubmit, submitting = false, initial }: Props) {
  const [name, setName] = useState(initial?.name ?? "");
  const [scope, setScope] = useState<AgentScope>(initial?.scope ?? "agent:read");
  const [days, setDays] = useState<number>(initial?.expiresInDays ?? 30);
  const [ack, setAck] = useState(initial?.acknowledgeAuto ?? false);
  const [nameError, setNameError] = useState(false);

  const auto = scope === "agent:auto";
  const choices = EXPIRY.filter((d) => !auto || d <= AUTO_MAX_DAYS);
  const effectiveDays = auto ? Math.min(days, AUTO_MAX_DAYS) : days;
  const blocked = auto && !ack;

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (blocked || submitting) return;
    if (name.trim() === "") {
      setNameError(true);
      return;
    }
    onSubmit({ name: name.trim(), scope, expiresInDays: effectiveDays, acknowledgeAuto: auto && ack });
  }

  return (
    <div className={`${card} mb-6`}>
      <div className={cardHeader}>
        <h2 className={cardTitle}>Create an agent token</h2>
      </div>
      <form onSubmit={handleSubmit} className="grid grid-cols-1 gap-5 p-6" data-testid="agent-mint-form">
        <div>
          <label htmlFor="agent-mint-name" className={label}>Name</label>
          <input
            id="agent-mint-name"
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              if (nameError) setNameError(false);
            }}
            className={`${input} sm:max-w-md`}
            maxLength={100}
            placeholder="Claude Desktop at home"
            aria-invalid={nameError}
            aria-describedby={nameError ? "agent-mint-name-error" : undefined}
          />
          {nameError && (
            <p id="agent-mint-name-error" role="alert" className="mt-1 text-xs text-danger">
              Give the token a name so you can recognize it later.
            </p>
          )}
        </div>

        <fieldset>
          <legend className={label}>Access</legend>
          <div className="mt-1 grid grid-cols-1 gap-2 md:grid-cols-3">
            {(Object.keys(SCOPE_INFO) as AgentScope[]).map((s) => (
              <label
                key={s}
                className={`flex cursor-pointer items-start gap-3 rounded-md border px-3 py-2.5 transition-colors ${
                  scope === s ? "border-accent bg-accent-dim" : "border-border hover:border-border-strong"
                }`}
              >
                <input
                  type="radio"
                  name="agent-mint-scope"
                  value={s}
                  checked={scope === s}
                  onChange={() => setScope(s)}
                  className="mt-0.5 accent-accent"
                />
                <span>
                  <span className="block text-sm font-medium text-text-primary">{SCOPE_INFO[s].label}</span>
                  <span className="block text-xs text-text-secondary">{SCOPE_INFO[s].hint}</span>
                </span>
              </label>
            ))}
          </div>
        </fieldset>

        {auto && (
          <div className={`${warningCls} space-y-3`} data-testid="agent-auto-ack">
            <p className="flex gap-2">
              <AlertTriangle aria-hidden className="mt-0.5 h-4 w-4 shrink-0" strokeWidth={1.75} />
              <span>
                An auto-apply token changes budgets and categories without asking you first. Every
                change is listed under Agent activity below, where you can revert it. Anyone holding
                the token can do the same, so keep it to one agent you trust.
              </span>
            </p>
            <label className="flex min-h-6 cursor-pointer items-center gap-2 text-text-primary">
              <input
                type="checkbox"
                checked={ack}
                onChange={(e) => setAck(e.target.checked)}
                className="h-4 w-4 accent-accent"
              />
              I understand changes apply without asking me
            </label>
          </div>
        )}

        <div>
          <label htmlFor="agent-mint-expiry" className={label}>Expires in</label>
          <select
            id="agent-mint-expiry"
            value={String(effectiveDays)}
            onChange={(e) => setDays(Number(e.target.value))}
            className={`${input} sm:max-w-[220px]`}
          >
            {choices.map((d) => (
              <option key={d} value={String(d)}>{d} days</option>
            ))}
          </select>
          <p className="mt-1 text-xs text-text-secondary">
            {auto ? "Auto-apply tokens last at most 30 days." : "Tokens expire on their own, after 90 days at most."}
          </p>
        </div>

        <div className="flex flex-col items-stretch gap-2 sm:flex-row sm:items-center sm:justify-end">
          {blocked && (
            <p id="agent-mint-blocked" className="text-xs text-text-secondary">
              Confirm the auto-apply notice to continue.
            </p>
          )}
          <button
            type="submit"
            aria-disabled={blocked || submitting}
            aria-describedby={blocked ? "agent-mint-blocked" : undefined}
            className={`${btnPrimary} w-full aria-disabled:opacity-50 sm:w-auto sm:min-h-0`}
          >
            {submitting ? "Creating…" : "Create token"}
          </button>
        </div>
      </form>
    </div>
  );
}
