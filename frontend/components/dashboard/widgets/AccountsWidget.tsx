"use client";

/**
 * AccountsWidget — thin wrapper around AccountTilesCard for the custom
 * dashboard canvas.
 *
 * Reads activeAccounts and pendingByAccount from the shared
 * DashboardDataProvider context. The Widget shape carries no data.
 *
 * FIX 9: account ordering mirrors LegacyDashboard — default account first,
 * then remaining accounts sorted alphabetically by name (locale-aware,
 * case-insensitive).
 */
import { useMemo } from "react";

import { useDashboard } from "@/components/dashboard/DashboardDataProvider";
import AccountTilesCard from "@/components/dashboard/AccountTile";

export default function AccountsWidget() {
  const { activeAccounts, pendingByAccount } = useDashboard();

  const orderedAccounts = useMemo(() => {
    const defaultAcct = activeAccounts.find((a) => a.is_default);
    const others = activeAccounts
      .filter((a) => !a.is_default)
      .slice()
      .sort((a, b) =>
        a.name.localeCompare(b.name, undefined, { sensitivity: "base" }),
      );
    return defaultAcct ? [defaultAcct, ...others] : others;
  }, [activeAccounts]);

  return (
    <AccountTilesCard
      accounts={orderedAccounts}
      pendingByAccount={pendingByAccount}
    />
  );
}
