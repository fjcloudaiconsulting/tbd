"use client";

// In-app assistant (TBD-581). The page gates on the org's plan and AI setup;
// the conversation itself lives in AssistantChat.

import Link from "next/link";

import AppShell from "@/components/AppShell";
import AssistantChat from "@/components/agent/AssistantChat";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { isAdmin } from "@/lib/auth";
import { useAiStatus } from "@/lib/hooks/use-ai-status";
import { card, pageTitle } from "@/lib/styles";

export default function AssistantPage() {
  const { user } = useAuth();
  const ai = useAiStatus();
  const agent = ai?.agent;
  const closed = !agent?.entitled || ai?.usage?.["assistant.turns"]?.limit === 0;

  let body: React.ReactNode;
  if (!ai) {
    body = <Spinner />;
  } else if (closed) {
    body = (
      <p className={`${card} max-w-2xl p-6 text-sm text-text-secondary`}>
        The assistant is not part of your organization&apos;s plan.
      </p>
    );
  } else if (!agent?.configured) {
    body = (
      <div className={`${card} max-w-2xl p-6 text-sm text-text-secondary`}>
        <p className="font-medium text-text-primary">The assistant needs an AI provider</p>
        <p className="mt-1">
          It answers with your organization&apos;s AI provider and model, chosen in settings.
        </p>
        {user && isAdmin(user) ? (
          <Link
            href="/settings/ai-providers"
            className="mt-3 inline-flex min-h-6 items-center text-text-primary underline underline-offset-2 hover:text-accent"
          >
            Set up an AI provider
          </Link>
        ) : (
          <p className="mt-3">Ask an admin of your organization to set one up.</p>
        )}
      </div>
    );
  } else {
    body = <AssistantChat />;
  }

  return (
    <AppShell>
      <h1 className={pageTitle}>Assistant</h1>
      {body}
    </AppShell>
  );
}
