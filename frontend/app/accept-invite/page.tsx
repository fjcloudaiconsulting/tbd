import type { Metadata } from "next";
import { Suspense } from "react";
import AcceptInviteBody from "@/components/auth/AcceptInviteBody";
import { pageSocialMeta, siteName } from "@/lib/site";

const description = "Accept your invitation to join an organization on The Better Decision.";

export const metadata: Metadata = {
  title: "Accept invitation",
  description,
  alternates: { canonical: "/accept-invite" },
  ...pageSocialMeta({
    title: `Accept invitation · ${siteName}`,
    description,
    path: "/accept-invite",
  }),
};

export default function AcceptInvitePage() {
  return (
    <Suspense fallback={null}>
      <AcceptInviteBody />
    </Suspense>
  );
}
