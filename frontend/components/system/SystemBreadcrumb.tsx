"use client";

import Link from "next/link";
import { ChevronRight } from "lucide-react";

// Shared trail back to the /system hub. Every /system/* sub-page renders
// this above its own page heading so the hub is reachable from anywhere in
// the platform-admin area (the hub itself has no sidebar entry — it is the
// index the sidebar links fan out from). `current` is the leaf label; it is
// not a link and carries aria-current so assistive tech announces the page.
export default function SystemBreadcrumb({ current }: { current: string }) {
  return (
    <nav aria-label="Breadcrumb" className="mb-4">
      <ol className="flex items-center gap-1.5 text-xs">
        <li>
          <Link
            href="/system"
            className="rounded-sm text-text-secondary transition-colors hover:text-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-bg"
          >
            System
          </Link>
        </li>
        <li aria-hidden="true" className="flex items-center">
          <ChevronRight className="h-3.5 w-3.5 text-text-muted" strokeWidth={2} />
        </li>
        <li aria-current="page" className="font-medium text-text-secondary">
          {current}
        </li>
      </ol>
    </nav>
  );
}
