"use client";

import { useState } from "react";

import FeedbackWidget from "@/components/feedback/FeedbackWidget";
import { useAuth } from "@/components/auth/AuthProvider";

/**
 * Footer-level "Give feedback" link. Logged-in only — renders nothing
 * when there is no authenticated user (spec section "Auth: Logged-in
 * users only").
 *
 * Mounted by `AppShellFooter`, which itself sits inside the AppShell
 * and is therefore only rendered for authenticated routes. The
 * `useAuth().user` gate is defense-in-depth: a future refactor that
 * promotes the footer to a shared layout still keeps the trigger off
 * for unauthed visitors.
 */
export default function FeedbackTrigger() {
  const { user } = useAuth();
  const [open, setOpen] = useState(false);

  if (!user) return null;

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="hover:text-text-primary"
        data-testid="feedback-trigger"
      >
        Give feedback
      </button>
      <FeedbackWidget open={open} onClose={() => setOpen(false)} />
    </>
  );
}
