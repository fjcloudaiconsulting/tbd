import { redirect } from "next/navigation";
import ReconcileClient from "./ReconcileClient";

// The server must not fetch here: the refresh cookie is shared by every tab,
// so a server read acts as whichever account signed in last (TBD-624).
// The island loads the batch client-side as the tab's own identity.
export default async function ReconcilePage({
  params,
}: {
  params: Promise<{ import_id: string }>;
}) {
  const { import_id } = await params;
  const batchId = Number(import_id);
  if (!Number.isFinite(batchId) || batchId <= 0) {
    redirect("/import");
  }
  return <ReconcileClient batchId={batchId} initialBatch={null} />;
}
