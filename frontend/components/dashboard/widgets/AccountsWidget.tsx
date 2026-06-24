"use client";

/**
 * AccountsWidget — thin wrapper around AccountTilesCard for the custom
 * dashboard canvas.
 *
 * Reads activeAccounts and pendingByAccount from the shared
 * DashboardDataProvider context. The Widget shape carries no data.
 */
import { useDashboard } from "@/components/dashboard/DashboardDataProvider";
import AccountTilesCard from "@/components/dashboard/AccountTile";

export default function AccountsWidget() {
  const { activeAccounts, pendingByAccount } = useDashboard();

  return (
    <AccountTilesCard
      accounts={activeAccounts}
      pendingByAccount={pendingByAccount}
    />
  );
}
