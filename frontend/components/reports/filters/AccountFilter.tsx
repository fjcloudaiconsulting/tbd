"use client";

/**
 * Account filter — chip picker over the org's accounts. Used in the
 * per-widget Filters tab (phase 4b made accounts widget-only; the
 * canvas filter bar is date-only).
 *
 * Fetches the org's accounts on mount via SWR and renders each as a
 * toggleable chip. Selecting / deselecting a chip flips its id in
 * the ``value`` list. Empty list = no filter (inherit / unfiltered).
 */
import useSWR from "swr";

import { apiFetch } from "@/lib/api";
import type { Account } from "@/lib/types";

interface Props {
  value: number[];
  onChange: (next: number[]) => void;
  /** Label shown above the chip strip. */
  label?: string;
  /** Aria-prefix on chip remove buttons + the empty hint. */
  ariaPrefix?: string;
}

const ACCOUNTS_SWR_KEY = "/api/v1/accounts?for=reports-filter";

async function fetchAccounts(): Promise<Account[]> {
  return apiFetch<Account[]>("/api/v1/accounts");
}

export default function AccountFilter({
  value,
  onChange,
  label = "Accounts",
  ariaPrefix = "Account",
}: Props) {
  const { data, error, isLoading } = useSWR<Account[]>(
    ACCOUNTS_SWR_KEY,
    fetchAccounts,
    { revalidateOnFocus: false },
  );

  // Deactivated accounts must not be selectable as report filters; the
  // shared /api/v1/accounts endpoint returns active + inactive (the
  // accounts management page needs the inactive ones to reactivate them).
  const accounts = (data ?? []).filter((a) => a.is_active);
  const selectedSet = new Set(value);

  function toggle(id: number) {
    const next = selectedSet.has(id)
      ? value.filter((v) => v !== id)
      : [...value, id];
    onChange(next);
  }

  return (
    <div className="flex flex-col gap-1" data-testid="account-filter">
      <span className="text-[10px] font-medium uppercase tracking-wider text-text-muted">
        {label}
      </span>
      {error ? (
        <div
          role="alert"
          data-testid="account-filter-error"
          className="text-xs text-danger"
        >
          Couldn&apos;t load accounts
        </div>
      ) : isLoading ? (
        <div
          data-testid="account-filter-loading"
          className="h-6 w-32 animate-pulse rounded bg-border/40"
        />
      ) : accounts.length === 0 ? (
        <span className="text-xs text-text-muted">No accounts yet</span>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {accounts.map((a) => {
            const active = selectedSet.has(a.id);
            return (
              <button
                key={a.id}
                type="button"
                data-testid={`account-filter-chip-${a.id}`}
                aria-pressed={active}
                aria-label={`${ariaPrefix} ${a.name}`}
                onClick={() => toggle(a.id)}
                className={`rounded-full border px-2.5 py-0.5 text-xs transition ${
                  active
                    ? "border-accent bg-accent text-accent-text"
                    : "border-border text-text-secondary hover:bg-surface-raised"
                }`}
              >
                {a.name}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
