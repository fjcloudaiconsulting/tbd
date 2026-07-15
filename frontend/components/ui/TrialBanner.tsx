"use client";

import Link from "next/link";
import { useState } from "react";
import { useAuth } from "@/components/auth/AuthProvider";
import type { User } from "@/lib/types";

interface Props {
  user: User;
}

export default function TrialBanner({ user }: Props) {
  const { billingUiEnabled } = useAuth();
  // Capture "now" once at mount so render stays pure (no Date.now() read
  // during render). Trial countdown is day-granular, so mount-time vs
  // render-time is indistinguishable.
  const [nowMs] = useState(() => Date.now());
  const { subscription_status, subscription_plan, trial_end } = user;

  // Customer-facing billing surface kill switch. Hides the trial /
  // upgrade chip in the AppShell header until the payment platform is
  // wired (BILLING_UI_ENABLED backend env, surfaced via /auth/status).
  if (!billingUiEnabled) return null;

  if (!subscription_status) return null;

  // Calculate days left for trial
  let daysLeft = 0;
  if (subscription_status === "trialing" && trial_end) {
    // Parse as UTC to avoid timezone drift; trial_end is a date string (YYYY-MM-DD)
    const endMs = Date.parse(trial_end + "T23:59:59Z");
    daysLeft = Math.max(0, Math.floor((endMs - nowMs) / 86_400_000));
  }

  // Trial active — plenty of time
  if (subscription_status === "trialing" && daysLeft > 3) {
    return (
      <div className="flex items-center gap-2 rounded-md border border-accent/30 bg-accent/10 px-3 py-1">
        <span className="text-xs font-medium text-accent">Pro Trial</span>
        <span className="text-[11px] text-accent/70">{daysLeft} days left</span>
      </div>
    );
  }

  // Trial expiring — urgent
  if (subscription_status === "trialing" && daysLeft <= 3) {
    return (
      <div className="flex items-center gap-2 rounded-md border border-warning/30 bg-warning-dim px-3 py-1">
        <span className="text-xs font-medium text-warning">Trial ending</span>
        <span className="text-[11px] text-warning/80">
          {daysLeft === 0 ? "today" : `${daysLeft} day${daysLeft !== 1 ? "s" : ""} left`}
        </span>
        <Link
          href="/settings/billing"
          className="text-[11px] font-medium text-warning underline hover:text-warning-hover"
        >
          Upgrade
        </Link>
      </div>
    );
  }

  // Free plan — show upgrade nudge
  if (subscription_plan === "free") {
    return (
      <div className="flex items-center gap-2 rounded-md border border-border bg-surface-raised px-3 py-1">
        <span className="text-xs text-text-muted">Free Plan</span>
        <Link
          href="/settings/billing"
          className="text-[11px] font-medium text-accent underline hover:text-accent-hover"
        >
          Upgrade
        </Link>
      </div>
    );
  }

  // Active paid plan — no banner needed
  return null;
}
