import ForecastPlansClient from "./ForecastPlansClient";

// The server must not fetch here: the refresh cookie is shared by every tab,
// so a server read acts as whichever account signed in last (TBD-624).
// The island loads everything client-side as the tab's own identity.
export default function ForecastPlansPage() {
  return (
    <ForecastPlansClient
      initialPeriods={[]}
      initialCategories={[]}
      initialPlan={null}
    />
  );
}
