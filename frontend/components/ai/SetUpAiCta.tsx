"use client";

import Link from "next/link";

const ADMIN_ROLES = new Set(["owner", "admin"]);

export function SetUpAiCta({ role, className }: { role: string | null; className?: string }) {
  if (role && ADMIN_ROLES.has(role)) {
    return (
      <Link href="/settings/ai-providers" className={className}>
        Set up AI
      </Link>
    );
  }
  return (
    <span className={className} aria-disabled="true">
      Set up AI (ask your organization admin)
    </span>
  );
}
