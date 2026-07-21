"use client";

import { FormEvent, useState } from "react";

import type { ApiTokenScope } from "@/lib/types";
import { btnPrimary, card, cardHeader, cardTitle, input, label } from "@/lib/styles";

import { DEFAULT_EXPIRY_DAYS, EXPIRY_PRESETS, SCOPE_OPTIONS } from "./expiry";

export interface MintFormValues {
  name: string;
  scope: ApiTokenScope;
  expiresInDays: number;
}

interface Props {
  onSubmit: (values: MintFormValues) => void;
  submitting?: boolean;
}

export default function MintTokenForm({ onSubmit, submitting = false }: Props) {
  const [name, setName] = useState("");
  const [scope, setScope] = useState<ApiTokenScope>("read");
  const [expiresInDays, setExpiresInDays] = useState<number>(DEFAULT_EXPIRY_DAYS);
  const [nameError, setNameError] = useState(false);

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (name.trim() === "") {
      setNameError(true);
      return;
    }
    setNameError(false);
    onSubmit({ name: name.trim(), scope, expiresInDays });
  }

  return (
    <div className={`${card} mb-6`}>
      <div className={cardHeader}>
        <h2 className={cardTitle}>Mint a token</h2>
      </div>
      <form
        onSubmit={handleSubmit}
        className="grid grid-cols-1 gap-5 p-6"
        data-testid="mint-form"
      >
        <div>
          <label htmlFor="mint-name" className={label}>
            Name
          </label>
          <input
            id="mint-name"
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              if (nameError) setNameError(false);
            }}
            className={input}
            maxLength={100}
            placeholder="broadcast cron"
            data-testid="mint-name"
          />
          {nameError && (
            <p className="mt-1 text-xs text-danger" data-testid="mint-name-error">
              Give the token a name so you can recognize it later.
            </p>
          )}
        </div>

        <fieldset>
          <legend className={label}>Scope</legend>
          <div className="mt-1 grid grid-cols-1 gap-2 sm:grid-cols-2">
            {SCOPE_OPTIONS.map((opt) => (
              <label
                key={opt.value}
                className={`flex cursor-pointer items-start gap-3 rounded-md border px-3 py-2.5 transition-colors ${
                  scope === opt.value
                    ? "border-accent bg-accent/5"
                    : "border-border hover:border-border-strong"
                }`}
              >
                <input
                  type="radio"
                  name="mint-scope"
                  value={opt.value}
                  checked={scope === opt.value}
                  onChange={() => setScope(opt.value)}
                  className="mt-0.5 accent-accent"
                  data-testid={`mint-scope-${opt.value}`}
                />
                <span>
                  <span className="block text-sm font-medium text-text-primary">
                    {opt.label}
                  </span>
                  <span className="block text-xs text-text-muted">{opt.hint}</span>
                </span>
              </label>
            ))}
          </div>
        </fieldset>

        <div>
          <label htmlFor="mint-expiry" className={label}>
            Expires in
          </label>
          <select
            id="mint-expiry"
            value={String(expiresInDays)}
            onChange={(e) => setExpiresInDays(Number(e.target.value))}
            className={`${input} sm:max-w-[220px]`}
            data-testid="mint-expiry"
          >
            {EXPIRY_PRESETS.map((days) => (
              <option key={days} value={String(days)}>
                {days} days
              </option>
            ))}
          </select>
          <p className="mt-1 text-[11px] text-text-muted">
            Tokens expire automatically. 90 days is the hard maximum.
          </p>
        </div>

        <div className="flex justify-end">
          <button
            type="submit"
            disabled={submitting}
            className={`${btnPrimary} w-full sm:w-auto sm:min-h-0`}
            data-testid="mint-submit"
          >
            {submitting ? "Generating…" : "Generate token"}
          </button>
        </div>
      </form>
    </div>
  );
}
