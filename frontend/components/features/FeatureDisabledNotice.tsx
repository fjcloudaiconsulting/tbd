"use client";

import Link from "next/link";

import { useAuth } from "@/components/auth/AuthProvider";
import { btnLink, card, cardTitle } from "@/lib/styles";

/**
 * The whole of what a page shows when its org switched the tool off (TBD-197).
 *
 * Small, centred, one line. No brass: DESIGN.md's One Brass Rule reserves the
 * accent for a page's primary action, and a page whose entire message is
 * absence has no primary action to promote. The admin affordance is a
 * `btnLink`, not a `btnPrimary`.
 *
 * Deliberately NOT a "Not available on your plan" upsell — this is the org's
 * own setting, not a paywall, and the copy has to read that way or the user
 * goes looking for a billing page that will not help them.
 */
export default function FeatureDisabledNotice({
  featureLabel,
}: {
  featureLabel: string;
}) {
  const { user } = useAuth();
  const orgName = user?.org_name ?? "your organization";
  const isAdmin = user?.role === "owner" || user?.role === "admin";

  return (
    <div
      className="mx-auto mt-12 max-w-md text-center"
      data-testid="feature-disabled-notice"
    >
      <div className={`${card} px-6 py-8`}>
        <h1 className={`${cardTitle} mb-3`}>{featureLabel}</h1>
        <p className="text-sm text-text-secondary">
          {featureLabel} isn&apos;t enabled for {orgName}. An organization admin
          can turn it back on in Settings &rarr; Planning tools.
        </p>
        {isAdmin && (
          <Link
            href="/settings/organization"
            className={`${btnLink} mt-4 inline-block`}
          >
            Go to organization settings
          </Link>
        )}
      </div>
    </div>
  );
}
