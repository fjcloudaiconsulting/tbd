"use client";

import Link from "next/link";
import { card } from "@/lib/styles";
import { formatAmount } from "@/lib/format";
import type { Account } from "@/lib/types";

export interface AccountTileProps {
  account: Account;
  hasPending: boolean;
}

// Compact identity/status/navigation tile for the dashboard accounts
// sidebar. The Forecast card is the numeric authority for Balance and
// expected month-end; this tile shows account name, type, status
// badges, and a click-through to /accounts. Balance is included as
// MUTED secondary text only — the Forecast card carries the primary
// number so the row doesn't read as redundant.
export default function AccountTile({ account, hasPending }: AccountTileProps) {
  const typeLabel = account.account_type_name ?? null;

  return (
    <Link
      href="/accounts"
      data-testid="account-tile"
      data-account-id={account.id}
      className={`${card} flex items-center justify-between gap-3 px-3 py-2.5 transition-colors hover:bg-surface-raised focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent`}
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
