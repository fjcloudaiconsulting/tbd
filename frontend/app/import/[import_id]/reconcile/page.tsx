import { redirect } from "next/navigation";
import { getServerSession } from "@/lib/auth-server";
import ReconcileClient from "./ReconcileClient";
import type { ImportBatchDetail } from "@/lib/types";

// L3.2 Wave 2B: post-import reconciliation inbox.
//
// The route lives at /import/[import_id]/reconcile and is authed-only;
// the RSC shell rejects unauthenticated requests at the server boundary
// (no flash of a protected screen). We fetch the batch detail once on
// the server with the access token and hand it to the client island as
// SWR fallbackData; the client re-fetches on action so the UI stays in
// sync with the server-side state machine after every transition.
//
// URL resolution mirrors the existing forecast-plans page so this
// module works in docker-compose, in DO App Platform, and from a
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

  const initialBatch = await fetchJSON<ImportBatchDetail>(
    `/api/v1/import/${batchId}`,
    session.accessToken,
  );

  return (
    <ReconcileClient batchId={batchId} initialBatch={initialBatch} />
  );
}
