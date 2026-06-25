"use client";

/**
 * AccountForecastWidget — thin wrapper around AccountMonthEndForecast for
 * the custom dashboard canvas.
 *
 * Reads forecast, period state, and account presence from the shared
 * DashboardDataProvider context. The Widget shape carries no data.
 */
import { useDashboard } from "@/components/dashboard/DashboardDataProvider";
import AccountMonthEndForecast from "@/components/dashboard/AccountMonthEndForecast";

export default function AccountForecastWidget() {
  const {
    accountMonthEndForecast,
    isCurrentSelectedPeriod,
    jumpToCurrentPeriod,
    activeAccounts,
    accountMonthEndForecastError,
  } = useDashboard();

  return (
    <AccountMonthEndForecast
      forecast={accountMonthEndForecast}
      isCurrentPeriod={isCurrentSelectedPeriod}
      onJumpToCurrent={jumpToCurrentPeriod}
      hasAnyAccounts={activeAccounts.length > 0}
      hasError={accountMonthEndForecastError}
    />
  );
}
