import { redirect } from "next/navigation";
import { getServerSession } from "@/lib/auth-server";
import { serverFetch } from "@/lib/server-fetch";
import ReconcileClient from "./ReconcileClient";
import type { ImportBatchDetail } from "@/lib/types";

// L3.2 Wave 2B: post-import reconciliation inbox.
//
// The route lives at /import/[import_id]/reconcile and is authed-only;
// the RSC shell rejects unauthenticated requests at the server boundary
// (no flash of a protected screen). We fetch the batch detail once on
// the server via the sanctioned `serverFetch` helper and hand it to the
// client island as SWR fallbackData; the client re-fetches on action so
// the UI stays in sync with the server-side state machine after every
// transition.

export default async function ReconcilePage({
  params,
}: {
  params: Promise<{ import_id: string }>;
}) {
  const session = await getServerSession();
  if (!session) redirect("/login");

  const { import_id } = await params;
  const batchId = Number(import_id);
  if (!Number.isFinite(batchId) || batchId <= 0) {
    redirect("/import");
  }

  const initialBatch = await serverFetch<ImportBatchDetail>(
    `/api/v1/import/${batchId}`,
    { accessToken: session.accessToken },
  );

  return (
    <ReconcileClient batchId={batchId} initialBatch={initialBatch} />
  );
}
