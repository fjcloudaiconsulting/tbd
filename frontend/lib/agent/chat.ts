// Assistant turn transport and transcript rules (TBD-581, spec TBD-558 4.7).
//
// The transcript lives in the browser and is sent whole each turn. The server
// accepts only user/assistant text, at most 40 messages and 64 KB of UTF-8,
// ending on a user message, with no blank entries.

import { apiFetch } from "@/lib/api";
import { readSse, type SseEvent } from "./sse";
import type { StagedAction } from "./types";

export const MAX_MESSAGES = 40;
export const MAX_BYTES = 64 * 1024;
// One message alone stays well under the byte cap even at 4 bytes a char.
export const MAX_INPUT_CHARS = 8000;

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

export type Entry =
  | { kind: "user"; id: string; text: string; failed?: boolean }
  | { kind: "assistant"; id: string; text: string }
  | { kind: "tool"; id: string; name: string; ok?: boolean; rows?: number | null; code?: string }
  | { kind: "preview"; id: string; tool: string; action: StagedAction; outcome?: string }
  | { kind: "error"; id: string; code: string };

const bytes = (s: string) => new TextEncoder().encode(s).length;

function previewNote(e: Extract<Entry, { kind: "preview" }>): string {
  const state =
    e.outcome === "done" ? "The user confirmed it and it was applied."
      : e.outcome === "cancelled" ? "The user cancelled it."
        : e.outcome ? "It was not applied."
          : "The user has not decided yet.";
  return `Proposed: ${e.action.summary}. ${state}`;
}

// The history to send: failed turns dropped, a staged change recorded as a
// server-written assistant line (never a blank one), same-role neighbours
// merged, then trimmed oldest-first by count and UTF-8 bytes so it always
// starts on a user message.
export function outgoing(entries: Entry[]): ChatMessage[] {
  const msgs: ChatMessage[] = [];
  for (const e of entries) {
    let m: ChatMessage | null = null;
    if (e.kind === "user" && !e.failed) m = { role: "user", content: e.text };
    else if (e.kind === "assistant") m = { role: "assistant", content: e.text };
    else if (e.kind === "preview") m = { role: "assistant", content: previewNote(e) };
    if (!m || !m.content.trim()) continue;
    const last = msgs[msgs.length - 1];
    if (last && last.role === m.role) last.content += `\n\n${m.content}`;
    else msgs.push(m);
  }
  let total = msgs.reduce((n, m) => n + bytes(m.content), 0);
  while (msgs.length > 1 && (msgs.length > MAX_MESSAGES || total > MAX_BYTES || msgs[0].role !== "user")) {
    total -= bytes(msgs.shift()!.content);
  }
  return msgs;
}

export interface Turn {
  events: AsyncGenerator<SseEvent>;
  cancel: () => void;
}

// Opens one turn. Refusals before the stream (busy, no provider, plan limit)
// reject as ApiResponseError with a code. The caller cancels with `cancel`:
// the request's abort signal no longer reaches the body once headers arrive.
export async function openTurn(messages: ChatMessage[]): Promise<Turn> {
  const res = await apiFetch<Response>("/api/v1/agent/chat", {
    method: "POST",
    body: JSON.stringify({ messages }),
    raw: true,
  });
  const reader = res.body!.getReader();
  return { events: readSse(reader), cancel: () => void reader.cancel().catch(() => {}) };
}

const ERRORS: Record<string, string> = {
  agent_busy: "Another assistant reply is still running for your organization. Try again in a moment.",
  agent_unavailable: "The assistant is unavailable right now. Try again in a moment.",
  round_limit: "The assistant could not finish this one. Try asking in smaller steps.",
  turn_timeout: "The assistant took too long to answer. Try again.",
  stream_interrupted: "The connection dropped before the reply finished. Try again.",
  plan_limit_reached: "You have used this period's assistant messages for your plan.",
  feature_not_enabled: "The assistant is not part of your plan.",
  user_inactive: "Your account is no longer active.",
  ai_routing_not_configured: "The assistant needs an AI provider. An admin can set one up in Settings, AI providers.",
  ai_capability_not_supported: "The configured AI model cannot use tools. An admin can pick another in Settings, AI providers.",
  ai_consent_required: "An admin needs to accept the AI provider terms in Settings, AI providers.",
  ai_hard_cap_exceeded: "Your organization's AI spending limit is reached for now.",
};

export function errorText(code: string, status?: number): string {
  if (ERRORS[code]) return ERRORS[code];
  if (status === 412) return "The assistant needs an AI provider. An admin can set one up in Settings, AI providers.";
  if (status === 402) return "Your organization's AI spending limit is reached for now.";
  return "Something went wrong. Try again.";
}
