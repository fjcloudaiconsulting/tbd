import { redirect } from "next/navigation";
import { getServerSessionResult } from "@/lib/auth-server";
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
//
// Session triage (PR #288): a 401/403 from /auth/verify means
// unauthenticated → redirect. A timeout / 5xx / network error is
// transient and renders the client island with `initialBatch=null` so
// SWR can re-fetch the batch detail after hydration. Without this
// triage, the previous code path either hung on loading.tsx or
// false-logged-out a user during a transient backend hiccup.

export default async function ReconcilePage({
  params,
}: {
  params: Promise<{ import_id: string }>;
}) {
  const sessionResult = await getServerSessionResult();
  if (sessionResult.kind === "unauthenticated") {
    redirect("/login");
  }

  const { import_id } = await params;
  const batchId = Number(import_id);
  if (!Number.isFinite(batchId) || batchId <= 0) {
    redirect("/import");
  }

  // Transient verify: render the client island with no seed data. The
  // client-side SWR layer (with apiFetch + bounded timeout from #286)
  // will issue the read after hydration. Better UX than spinning
  // forever or bouncing a logged-in user to /login on a transient
  // backend hiccup.
  if (sessionResult.kind === "transient") {
    return <ReconcileClient batchId={batchId} initialBatch={null} />;
  }

  const initialBatch = await serverFetch<ImportBatchDetail>(
    `/api/v1/import/${batchId}`,
    { accessToken: sessionResult.session.accessToken },
  );

  return <ReconcileClient batchId={batchId} initialBatch={initialBatch} />;
}
