"use client";

/**
 * OnTrackWidget — thin wrapper around OnTrackTile for the custom dashboard canvas.
 *
 * Reads all required props from the shared DashboardDataProvider context so
 * the Widget shape ({id,type,title,grid,config}) carries no data — the
 * provider owns the fetches.
 */
import { useDashboard } from "@/components/dashboard/DashboardDataProvider";
import OnTrackTile from "@/components/dashboard/OnTrackTile";

export default function OnTrackWidget() {
  const {
    forecast,
    forecastProjection,
    projectionFailed,
    projectionLoading,
    onRetryProjection,
    isPastSelectedPeriod,
    isFutureSelectedPeriod,
  } = useDashboard();

  return (
    <OnTrackTile
      forecastPlan={forecast}
      projection={forecastProjection}
      projectionFailed={projectionFailed}
      projectionLoading={projectionLoading}
      onRetryProjection={onRetryProjection}
      isPastPeriod={isPastSelectedPeriod}
      isFuturePeriod={isFutureSelectedPeriod}
    />
  );
}
