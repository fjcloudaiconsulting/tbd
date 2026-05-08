"use client";

import Link from "next/link";
import { card } from "@/lib/styles";
import { formatAmount } from "@/lib/format";
import type { Account } from "@/lib/types";

export interface AccountTilesCardProps {
  accounts: Account[];
  pendingByAccount: Record<number, number>;
}

// Compact identity/status/navigation column for the dashboard. ONE
// shared card with internal divider rows, mirroring the Forecast card
// idiom on the right side of the row. Each row is a click-through to
// /accounts. The Forecast card is the numeric authority; the muted
// balance hint here is secondary text only, NOT the primary visual
// anchor of the row.
export default function AccountTilesCard({
  accounts,
  pendingByAccount,
}: AccountTilesCardProps) {
  if (accounts.length === 0) return null;

  return (
    <section className={`${card} overflow-hidden`} data-testid="account-tiles-card">
      <div className="divide-y divide-border-subtle">
        {accounts.map((account) => (
          <AccountTileRow
            key={account.id}
            account={account}
            hasPending={(pendingByAccount[account.id] ?? 0) !== 0}
          />
        ))}
      </div>
    </section>
  );
}

export interface AccountTileRowProps {
  account: Account;
  hasPending: boolean;
}

export function AccountTileRow({ account, hasPending }: AccountTileRowProps) {
  const typeLabel = account.account_type_name ?? null;

  return (
    <Link
      href="/accounts"
      data-testid="account-tile"
      data-account-id={account.id}
      className="flex items-center justify-between gap-3 px-3 py-2.5 transition-colors hover:bg-surface-raised focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
    >
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <p className="truncate text-sm font-medium text-text-primary">
            {account.name}
          </p>
          {account.is_default && (
            <span className="shrink-0 rounded border border-border px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-text-secondary">
              Primary
            </span>
          )}
        </div>
        <div className="mt-0.5 flex items-center gap-2 text-[11px] text-text-muted">
          {typeLabel && <span className="truncate">{typeLabel}</span>}
          {typeLabel && <span aria-hidden="true">·</span>}
          <span className="uppercase tracking-wider">{account.currency}</span>
          {hasPending && (
            <>
              <span aria-hidden="true">·</span>
              <span
                className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-amber-500"
                aria-label="Has pending transactions"
              >
                Pending
              </span>
            </>
          )}
        </div>
      </div>
      <p
        className="shrink-0 text-[11px] tabular-nums text-text-muted"
        aria-label="Current balance, secondary"
      >
        {formatAmount(account.balance)}
      </p>
    </Link>
  );
}
