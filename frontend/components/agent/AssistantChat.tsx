"use client";

// The in-app assistant (TBD-581, spec TBD-558 4.7). Model output is plain
// text, rendered as a text node: no markdown, no links, no images. Only the
// server-rendered activity lines and preview cards carry structure.

import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { Check, CircleAlert, Search } from "lucide-react";

import { ApiResponseError } from "@/lib/api";
import {
  MAX_INPUT_CHARS, errorText, openTurn, outgoing, type Entry, type Turn,
} from "@/lib/agent/chat";
import { toolLabel } from "@/lib/agent/present";
import type { StagedAction } from "@/lib/agent/types";
import { maskMoneyText } from "@/lib/format";
import { useBalancesHidden } from "@/lib/hooks/use-org-currency";
import {
  badgeError, btnSecondary, error as errorCls, filterChip, filterChipOff, input,
} from "@/lib/styles";

import PreviewCard, { type Outcome } from "./PreviewCard";

const EXAMPLES = [
  "How much have I spent on groceries this month?",
  "Which budgets am I over this month?",
  "What does my forecast say for the end of the month?",
];

function ToolLine({ e }: { e: Extract<Entry, { kind: "tool" }> }) {
  const pending = e.ok === undefined;
  const Icon = pending ? Search : e.ok ? Check : CircleAlert;
  const detail = pending
    ? "working"
    : e.ok
      ? typeof e.rows === "number" ? `${e.rows} ${e.rows === 1 ? "result" : "results"}` : "done"
      : "failed";
  return (
    <p className="flex items-center gap-2 text-xs text-text-secondary">
      <Icon aria-hidden className={`h-3.5 w-3.5 shrink-0 ${e.ok === false ? "text-danger" : ""}`} strokeWidth={1.75} />
      <span>{toolLabel(e.name)}</span>
      <span aria-hidden className="text-text-muted">·</span>
      <span className={e.ok === false ? "text-danger" : "text-text-muted"}>{detail}</span>
    </p>
  );
}

export default function AssistantChat() {
  useBalancesHidden(); // repaint model text on Hide balances (TBD-527)
  const [entries, setEntries] = useState<Entry[]>([]);
  const [draft, setDraft] = useState("");
  const [streaming, setStreaming] = useState(false);
  const turnRef = useRef<Turn | null>(null);
  const turnBusy = useRef(false);
  const stoppedRef = useRef(false);
  const seq = useRef(0);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const endRef = useRef<HTMLDivElement>(null);

  const id = () => `e${++seq.current}`;

  // Cancel a running turn when the page goes away, so it frees the org's
  // turn lock instead of running on unseen.
  useEffect(() => () => turnRef.current?.cancel(), []);

  useEffect(() => {
    const last = entries[entries.length - 1];
    // A preview takes focus itself; scrolling to the end would push its heading away.
    if (last && last.kind !== "preview") endRef.current?.scrollIntoView?.({ block: "end" });
  }, [entries]);

  async function send(raw: string) {
    const text = raw.trim();
    if (!text || turnBusy.current) return;
    turnBusy.current = true;
    const user: Entry = { kind: "user", id: id(), text };
    const history = [...entries.filter((e) => !(e.kind === "user" && e.failed) && e.kind !== "error"), user];
    setEntries(history);
    setDraft("");
    setStreaming(true);
    stoppedRef.current = false;

    const add = (e: Entry) => setEntries((prev) => [...prev, e]);
    let produced = false;
    let done = false;
    let code: string | null = null;
    let status: number | undefined;
    let lastTool = "";
    try {
      const turn = await openTurn(outgoing(history));
      turnRef.current = turn;
      for await (const ev of turn.events) {
        let data: Record<string, unknown>;
        try {
          data = JSON.parse(ev.data);
        } catch {
          continue;
        }
        if (ev.event === "message") {
          const t = typeof data.text === "string" ? data.text : "";
          if (t.trim()) {
            produced = true;
            add({ kind: "assistant", id: id(), text: t });
          }
        } else if (ev.event === "tool_call") {
          lastTool = String(data.name ?? "");
          add({ kind: "tool", id: id(), name: lastTool });
        } else if (ev.event === "tool_result") {
          setEntries((prev) => {
            const i = prev.findLastIndex((e) => e.kind === "tool" && e.name === data.name && e.ok === undefined);
            if (i === -1) return prev;
            const next = [...prev];
            next[i] = {
              ...(prev[i] as Extract<Entry, { kind: "tool" }>),
              ok: data.ok === true,
              rows: typeof data.rows === "number" ? data.rows : null,
              code: typeof data.code === "string" ? data.code : undefined,
            };
            return next;
          });
        } else if (ev.event === "preview") {
          produced = true;
          const tool = lastTool;
          // The staged write replaces its own "working" activity line.
          setEntries((prev) => [
            ...prev.filter((e) => !(e.kind === "tool" && e.name === tool && e.ok === undefined)),
            { kind: "preview", id: id(), tool, action: data.action as StagedAction },
          ]);
        } else if (ev.event === "error") {
          code = String(data.code ?? "internal_error");
        } else if (ev.event === "done") {
          done = true;
        }
      }
      if (!done && !stoppedRef.current) code = code ?? "stream_interrupted";
    } catch (err) {
      code = err instanceof ApiResponseError ? err.code ?? "request_failed" : "stream_interrupted";
      status = err instanceof ApiResponseError ? err.status : undefined;
    } finally {
      turnRef.current = null;
      turnBusy.current = false;
      setStreaming(false);
    }
    if (stoppedRef.current && !produced) {
      setEntries((prev) => prev.map((e) => (e.id === user.id ? { ...user, failed: true } : e)));
      return;
    }
    if (code) {
      const failed = !produced;
      setEntries((prev) => [
        ...prev.map((e) => (failed && e.id === user.id ? { ...user, failed: true } : e)),
        { kind: "error", id: id(), code: status ? `${status}:${code}` : code },
      ]);
    }
  }

  function stop() {
    stoppedRef.current = true;
    turnRef.current?.cancel();
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    void send(draft);
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send(draft);
    }
  }

  function decided(entryId: string, outcome: Outcome) {
    setEntries((prev) => prev.map((e) => (e.id === entryId && e.kind === "preview" ? { ...e, outcome } : e)));
  }

  const lastFailed = [...entries].reverse().find((e) => e.kind === "user" && e.failed);
  // Only the newest undecided card carries the brass Apply.
  const newestOpen = [...entries].reverse().find((e) => e.kind === "preview" && !e.outcome)?.id;

  return (
    <div className="mx-auto flex h-[calc(100dvh-13rem)] min-h-[26rem] max-w-3xl flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto pr-1" data-testid="transcript-pane">
      {entries.length === 0 && (
        <div className="mb-8">
          <p className="max-w-[60ch] text-sm text-text-secondary">
            Ask about your accounts, budgets, transactions and forecast. When the assistant
            suggests a change to a budget or a category, nothing happens until you confirm it.
          </p>
          <ul className="mt-5 flex flex-wrap gap-2" aria-label="Example questions">
            {EXAMPLES.map((q) => (
              <li key={q}>
                <button
                  type="button"
                  className={`${filterChip} ${filterChipOff} px-3 py-1.5 text-sm`}
                  onClick={() => {
                    setDraft(q);
                    inputRef.current?.focus();
                  }}
                >
                  {q}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div role="log" aria-live="polite" aria-relevant="additions" aria-label="Conversation">
      <ol className="space-y-5 pb-6">
        {entries.map((e) => {
          if (e.kind === "user") {
            return (
              <li key={e.id} className="flex flex-col items-end">
                <span className="sr-only">You said:</span>
                <p className="max-w-[85%] whitespace-pre-wrap rounded-lg bg-surface-raised px-4 py-2.5 text-sm text-text-primary [overflow-wrap:anywhere]">
                  {e.text}
                </p>
                {e.failed && <span className={`${badgeError} mt-1`}>Not sent</span>}
              </li>
            );
          }
          if (e.kind === "assistant") {
            return (
              <li key={e.id}>
                <span className="sr-only">Assistant said:</span>
                <p
                  className="max-w-[70ch] whitespace-pre-wrap text-sm leading-relaxed text-text-primary [overflow-wrap:anywhere]"
                  data-testid="assistant-text"
                >
                  {maskMoneyText(e.text)}
                </p>
              </li>
            );
          }
          if (e.kind === "tool") return <li key={e.id}><ToolLine e={e} /></li>;
          if (e.kind === "preview") {
            return (
              <li key={e.id}>
                <PreviewCard
                  tool={e.tool}
                  action={e.action}
                  autoFocus
                  emphasis={e.id === newestOpen}
                  onDecided={(o) => decided(e.id, o)}
                />
              </li>
            );
          }
          const [statusPart, codePart] = e.code.includes(":") ? e.code.split(":") : ["", e.code];
          return (
            <li key={e.id} className={`${errorCls} flex flex-wrap items-center justify-between gap-3`}>
              <span>{errorText(codePart, statusPart ? Number(statusPart) : undefined)}</span>
              {lastFailed && !streaming && e === entries[entries.length - 1] && (
                <button
                  type="button"
                  className={`${btnSecondary} min-h-[36px] py-1.5`}
                  onClick={() => void send((lastFailed as Extract<Entry, { kind: "user" }>).text)}
                >
                  Retry message
                </button>
              )}
            </li>
          );
        })}
        {streaming && (
          <li className="flex items-center gap-2 text-xs text-text-muted">
            <span aria-hidden className="h-1.5 w-1.5 animate-pulse rounded-full bg-text-muted motion-reduce:animate-none" />
            Working on it
          </li>
        )}
      </ol>
      </div>
      <div ref={endRef} />
      </div>

      <form onSubmit={onSubmit} className="border-t border-border pt-3">
        <label htmlFor="assistant-input" className="sr-only">Message the assistant</label>
        <div className="flex items-end gap-2">
          <textarea
            id="assistant-input"
            ref={inputRef}
            value={draft}
            onChange={(ev) => setDraft(ev.target.value)}
            onKeyDown={onKeyDown}
            readOnly={streaming}
            aria-disabled={streaming}
            maxLength={MAX_INPUT_CHARS}
            rows={2}
            placeholder="Ask about your money"
            className={`${input} min-h-[44px] resize-y aria-disabled:opacity-60`}
          />
          {/* One slot, so focus on Send survives the swap to Stop. */}
          <button
            type={streaming ? "button" : "submit"}
            onClick={streaming ? stop : undefined}
            className={`${btnSecondary} min-h-[44px] shrink-0`}
          >
            {streaming ? "Stop response" : "Send"}
          </button>
        </div>
        <p className="mt-1.5 text-xs text-text-muted">
          Enter sends, Shift+Enter starts a new line. The conversation is not saved when you leave this page.
        </p>
      </form>
    </div>
  );
}
