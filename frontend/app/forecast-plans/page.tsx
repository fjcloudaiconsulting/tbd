import { redirect } from "next/navigation";
import { getServerSessionResult } from "@/lib/auth-server";
import { serverFetch } from "@/lib/server-fetch";
import ForecastPlansClient from "./ForecastPlansClient";
import type { BillingPeriod, Category, ForecastPlan } from "@/lib/types";

// First consumer of the RSC auth foundation (PR #210 / #211 / #212).
// `getServerSessionResult()` reads the refresh cookie, validates it
// server-side against the backend, and returns a discriminated result:
//
//   - "authenticated" → render the happy path with server-fetched
//     initial data as SWR fallbackData.
//   - "unauthenticated" (401/403 from /auth/verify) → redirect to /login.
//   - "transient" (timeout / 5xx / network / invalid payload) → render
//     the client island with empty fallback data so the page exits the
//     loading.tsx Suspense and the client-side SWR layer can re-fetch
//     after hydration with its own bounded-timeout `apiFetch` and
//     refresh contract. This prevents the "stuck on spinner forever"
//     class of bug (#288) without producing a false-logout during a
//     backend hiccup.
//
// We then issue the three initial reads in parallel (categories, billing
// periods, and the plan for the visible period) via the sanctioned
// `serverFetch` helper, and hand the results down as initial props. The
// client uses them as SWR `fallbackData` for the plan so the page paints
// immediately and only re-fetches when the user navigates periods or
// mutates the plan.
//
// The `ensure-future` POST (a side-effect write that pre-creates forward
// billing-period stubs) intentionally stays in the client. RSC fetches
// should be idempotent reads, and the existing client already runs
// ensure-future once-per-session before loading periods.

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
  const sessionResult = await getServerSessionResult();
  if (sessionResult.kind === "unauthenticated") {
    redirect("/login");
  }

  // Transient verify failure: render the client island with safe empty
  // fallback data. The client uses `apiFetch` (#286) with its own
  // bounded timeout + the discriminated `RefreshResult` (#287) so it
  // will retry after hydration without false-logout. This is the
  // architect-locked direction for the #288 hotfix: expected-error
  // fallback handling rather than throwing to an error.tsx boundary.
  if (sessionResult.kind === "transient") {
    return (
      <ForecastPlansClient
        initialPeriods={[]}
        initialCategories={[]}
        initialPlan={null}
      />
    );
  }

  const { session } = sessionResult;

  // `Promise.allSettled` so a single fetch transient (e.g. the
  // categories endpoint is rate-limited but billing-periods responds
  // fine) doesn't strand the whole render in the loading state. We
  // pull `data` for fulfilled OKs and pass empty arrays for
  // rejected/null entries; the client island re-fetches on hydration
  // anyway, so a partial first paint is strictly better than the
  // previous "any one fetch hangs → entire page hangs" coupling.
  const [categoriesSettled, periodsSettled] = await Promise.allSettled([
    serverFetch<Category[]>("/api/v1/categories", {
      accessToken: session.accessToken,
    }),
    serverFetch<BillingPeriod[]>("/api/v1/settings/billing-periods", {
      accessToken: session.accessToken,
    }),
  ]);

  const categories =
    categoriesSettled.status === "fulfilled"
      ? (categoriesSettled.value ?? [])
      : [];
  const periodList =
    periodsSettled.status === "fulfilled"
      ? (periodsSettled.value ?? [])
      : [];

  const initialPeriod = pickCurrentPeriod(periodList);

  // The plan endpoint is `get_or_create` — passing a period that doesn't
  // yet have a plan auto-creates a draft. That matches the pre-RSC
  // client's first-load behavior; preserving UX is the goal of this PR.
  // Wrapped in a try/catch via `serverFetch`'s null contract: a null
  // here just means the client island gets no fallbackData and SWR
  // will fetch on hydration.
  const initialPlan = initialPeriod
    ? await serverFetch<ForecastPlan>(
        `/api/v1/forecast-plans?period_start=${initialPeriod.start_date}`,
        { accessToken: session.accessToken },
      )
    : null;

  return (
    <ForecastPlansClient
      initialPeriods={periodList}
      initialCategories={categories}
      initialPlan={initialPlan}
    />
  );
}
