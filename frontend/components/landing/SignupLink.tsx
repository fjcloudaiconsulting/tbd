"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { signupHref } from "@/lib/links";
import { trackRegisterClick, type SignupCtaLocation } from "@/lib/analytics";

// Single signup-CTA primitive for the apex landing surface. Owns the
// register_click fire so the (server-rendered) call sites need not be client
// components. Navigation is the normal <Link> behaviour — the event rides
// GA4's sendBeacon and does not block it.
export default function SignupLink({
  location,
  className,
  children,
}: {
  location: SignupCtaLocation;
  className?: string;
  children: ReactNode;
}) {
  return (
    <Link
      href={signupHref()}
      className={className}
      onClick={() => trackRegisterClick(location)}
    >
      {children}
    </Link>
  );
}
