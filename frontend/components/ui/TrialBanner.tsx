"use client";

import Link from "next/link";
import type { User } from "@/lib/types";

interface Props {
  user: User;
}

export default function TrialBanner({ user }: Props) {
  const { subscription_status, subscription_plan, trial_end } = user;

  if (!subscription_status) return null;

  // Calculate days left for trial
  let daysLeft = 0;
  if (subscription_status === "trialing" && trial_end) {
    const end = new Date(trial_end + "T23:59:59");
    const now = new Date();
    daysLeft = Math.max(0, Math.ceil((end.getTime() - now.getTime()) / (1000 * 60 * 60 * 24)));
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
      <div className="flex items-center gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-1">
        <span className="text-xs font-medium text-amber-400">Trial ending</span>
        <span className="text-[11px] text-amber-300">
          {daysLeft === 0 ? "today" : `${daysLeft} day${daysLeft !== 1 ? "s" : ""} left`}
        </span>
        <Link
          href="/settings/billing"
          className="text-[11px] font-medium text-amber-400 underline hover:text-amber-300"
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
