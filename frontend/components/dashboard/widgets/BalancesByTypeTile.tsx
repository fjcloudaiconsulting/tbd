"use client";

/**
 * BalancesByTypeTile — "Balances by type" dashboard tile (Phase 0 of the
 * configurable-dashboard-widgets roadmap). Consolidates active-account
 * balances into one row per ACCOUNT TYPE, each row showing the type name,
 * account count, and a subtotal PER CURRENCY.
 *
 * Design rules (architect + design reviewed, spec
 * 2026-07-23-dashboard-phase0-balances-by-type-design.md):
 *  - Group by ``account_type_id`` (NOT a hardcoded slug allowlist) so accounts
 *    on custom/non-system types (``account_type_slug === null``) are included;
 *    a slug allowlist would silently drop their balances.
 *  - Never sum across currencies, and never net across types within a currency
 *    (that would be an accidental net-worth-per-currency figure). Each cell is
 *    a single (type x currency) subtotal.
 *  - ``Number(a.balance)`` before summing — the wire value is a string despite
 *    the TS ``number`` type.
 *  - Liabilities keep their stored sign (negative) rendered in text-primary
 *    with NO status color and NO "(owed)" suffix — the sign already carries the
 *    meaning (house rule, accounts/page.tsx). Row icons are text-secondary, not
 *    brass (One Brass Rule).
 */
import { useMemo } from "react";
import Link from "next/link";
import {
  CreditCard,
  Landmark,
  PiggyBank,
  TrendingUp,
  Wallet,
  type LucideIcon,
} from "lucide-react";

import { useDashboard } from "@/components/dashboard/DashboardDataProvider";
import { formatAmount } from "@/lib/format";
import { card, cardHeader, cardTitle } from "@/lib/styles";

/** Assets first, liabilities last, custom/unknown types after. */
const SLUG_ORDER: Record<string, number> = {
  checking: 0,
  savings: 1,
  cash: 2,
  investment: 3,
  credit_card: 4,
  loan: 5,
};

function orderIndex(slug: string | null): number {
  return slug != null && slug in SLUG_ORDER ? SLUG_ORDER[slug] : 99;
}

const ICON_BY_SLUG: Record<string, LucideIcon> = {
  checking: Landmark,
  savings: PiggyBank,
  cash: Wallet,
  investment: TrendingUp,
  credit_card: CreditCard,
  loan: Landmark,
};

function iconForSlug(slug: string | null): LucideIcon {
  return (slug != null && ICON_BY_SLUG[slug]) || Wallet;
}

/** Max currency lines shown per row before collapsing to "+N more". */
const MAX_CURRENCY_LINES = 2;

interface CurrencySubtotal {
  currency: string;
  total: number;
}

interface TypeGroup {
  typeId: number;
  name: string;
  slug: string | null;
  count: number;
  /** One subtotal per currency, sorted by magnitude desc. */
  byCurrency: CurrencySubtotal[];
}

/** Spell the sign for screen readers ("minus 850.00 EUR"). */
function spokenAmount({ currency, total }: CurrencySubtotal): string {
  const sign = total < 0 ? "minus " : "";
  return `${sign}${formatAmount(Math.abs(total))} ${currency}`;
}

export default function BalancesByTypeTile() {
  const { activeAccounts } = useDashboard();

  const groups = useMemo<TypeGroup[]>(() => {
    const byType = new Map<
      number,
      { name: string; slug: string | null; count: number; sums: Map<string, number> }
    >();

    for (const a of activeAccounts) {
      let g = byType.get(a.account_type_id);
      if (!g) {
        g = {
          name: a.account_type_name,
          slug: a.account_type_slug,
          count: 0,
          sums: new Map<string, number>(),
        };
        byType.set(a.account_type_id, g);
      }
      g.count += 1;
      g.sums.set(a.currency, (g.sums.get(a.currency) ?? 0) + Number(a.balance));
    }

    const arr: TypeGroup[] = [...byType.entries()].map(([typeId, g]) => ({
      typeId,
      name: g.name,
      slug: g.slug,
      count: g.count,
      byCurrency: [...g.sums.entries()]
        .map(([currency, total]) => ({ currency, total }))
        .sort((x, y) => Math.abs(y.total) - Math.abs(x.total)),
    }));

    arr.sort(
      (a, b) => orderIndex(a.slug) - orderIndex(b.slug) || a.name.localeCompare(b.name),
    );
    return arr;
  }, [activeAccounts]);

  return (
    <div className={`${card} flex flex-col overflow-hidden`}>
      <div className={`flex items-center justify-between ${cardHeader}`}>
        <h2 className={cardTitle}>Balances by type</h2>
        <Link
          href="/accounts"
          className="text-xs text-text-secondary underline underline-offset-2 hover:text-text-primary"
        >
          Accounts
        </Link>
      </div>

      {groups.length === 0 ? (
        <div className="px-5 py-6 text-center text-sm text-text-muted">
          No accounts yet.{" "}
          <Link
            href="/accounts"
            className="text-text-primary underline underline-offset-2 hover:text-text-secondary"
          >
            Add one
          </Link>
        </div>
      ) : (
        <div className="divide-y divide-border-subtle">
          {groups.map((g) => {
            const Icon = iconForSlug(g.slug);
            const visible = g.byCurrency.slice(0, MAX_CURRENCY_LINES);
            const hiddenCount = g.byCurrency.length - visible.length;
            const countLabel = `${g.count} ${g.count === 1 ? "account" : "accounts"}`;
            const ariaLabel = `${g.name}, ${countLabel}, ${g.byCurrency
              .map(spokenAmount)
              .join(", ")}`;

            return (
              <Link
                key={g.typeId}
                href="/accounts"
                aria-label={ariaLabel}
                data-testid="balances-by-type-row"
                className="flex items-start justify-between gap-3 px-4 py-3 transition-colors hover:bg-surface-raised focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <Icon
                      aria-hidden="true"
                      strokeWidth={1.5}
                      className="h-4 w-4 shrink-0 text-text-secondary"
                    />
                    <p className="truncate text-sm font-medium text-text-primary">
                      {g.name}
                    </p>
                  </div>
                  <p className="mt-0.5 pl-6 text-[11px] text-text-muted">
                    {countLabel}
                  </p>
                </div>

                <div
                  aria-hidden="true"
                  className="flex shrink-0 flex-col items-end gap-0.5"
                >
                  {visible.map((c) => (
                    <p key={c.currency} className="text-sm tabular-nums text-text-primary">
                      {formatAmount(c.total)}{" "}
                      <span className="text-[11px] uppercase tracking-wider text-text-muted">
                        {c.currency}
                      </span>
                    </p>
                  ))}
                  {hiddenCount > 0 && (
                    <p className="text-[11px] text-text-muted">+{hiddenCount} more</p>
                  )}
                </div>
              </Link>
            );
          })}
        </div>
      )}
    </div>
  );
}
