import { redirect } from "next/navigation";
import { getServerSession } from "@/lib/auth-server";
import ForecastPlansClient from "./ForecastPlansClient";
import type { BillingPeriod, Category, ForecastPlan } from "@/lib/types";

// First consumer of the RSC auth foundation (PR #210 / #211 / #212).
// `getServerSession()` reads the refresh cookie, validates it server-side
// against the backend, and returns `{ user, accessToken }` or null. If
// null, we bounce to /login here so the protected client component never
// has to deal with an unauthenticated state.
//
// We then issue the three initial reads in parallel (categories, billing
// periods, and the plan for the visible period) with the access token, and
// hand the results down as initial props. The client uses them as SWR
// `fallbackData` for the plan so the page paints immediately and only
// re-fetches when the user navigates periods or mutates the plan.
//
// The `ensure-future` POST (a side-effect write that pre-creates forward
// billing-period stubs) intentionally stays in the client. RSC fetches
// should be idempotent reads, and the existing client already runs
// ensure-future once-per-session before loading periods.
//
// URL resolution mirrors `lib/auth-server.ts` so this module works in
// docker-compose (BACKEND_INTERNAL_URL=http://backend:8000), in DO App
// Platform (BACKEND_INTERNAL_URL=${backend.PRIVATE_URL}), and from a
// developer's host shell against a locally-running backend.

const SERVER_API_URL =
  process.env.BACKEND_INTERNAL_URL ||
  process.env.NEXT_PUBLIC_API_URL ||
  "http://localhost:8000";

async function fetchJSON<T>(
  path: string,
  accessToken: string,
): Promise<T | null> {
  try {
    const res = await fetch(`${SERVER_API_URL}${path}`, {
      headers: { Authorization: `Bearer ${accessToken}` },
      cache: "no-store",
    });
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

// The existing client picks the "current" period (the open one with
// end_date === null) and falls back to index 0 when there isn't one. We
// reproduce that here so the server-fetched plan matches what the client
// would have picked on first paint.
function pickCurrentPeriod(periods: BillingPeriod[]): BillingPeriod | null {
  if (periods.length === 0) return null;
  const open = periods.find((p) => p.end_date === null);
  return open ?? periods[0];
}

export default async function ForecastPlansPage() {
  const session = await getServerSession();
  if (!session) redirect("/login");

  const [categories, periods] = await Promise.all([
    fetchJSON<Category[]>("/api/v1/categories", session.accessToken),
    fetchJSON<BillingPeriod[]>(
      "/api/v1/settings/billing-periods",
      session.accessToken,
    ),
  ]);

  const periodList = periods ?? [];
  const initialPeriod = pickCurrentPeriod(periodList);

  // The plan endpoint is `get_or_create` — passing a period that doesn't
  // yet have a plan auto-creates a draft. That matches the pre-RSC
  // client's first-load behavior; preserving UX is the goal of this PR.
  const initialPlan = initialPeriod
    ? await fetchJSON<ForecastPlan>(
        `/api/v1/forecast-plans?period_start=${initialPeriod.start_date}`,
        session.accessToken,
      )
    : null;

  return (
    <ForecastPlansClient
      initialPeriods={periodList}
      initialCategories={categories ?? []}
      initialPlan={initialPlan}
    />
  );
}
