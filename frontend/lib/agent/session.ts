// The assistant conversation outlives the page (TBD-614). A module-scoped store,
// mirrored to sessionStorage, so a turn's writes after unmount still land and a
// remount reads the same transcript. The browser tab is the only place it lives.
//
// Owner = "<user.id>:<user.org_id>". getSnapshot is PURE; only the four
// mutators below change the store, and each one emits. Every asynchronous
// writer captures `generation()` first and writes through updateEntries, which
// drops the write when the generation moved (sign-out, owner switch, New chat).

import type { Entry } from "./chat";
import type { AgentActionRow } from "./types";

const KEY = "tbd-assistant:v1";
const IDLE_MS = 2 * 60 * 60 * 1000;
const KEEP = 100;
const OUTCOMES = ["done", "cancelled", "expired", "failed", "decided"];

export interface Snapshot {
  owner: string | null;
  entries: Entry[];
  lastMessageAt: number | null;
}

const EMPTY: Snapshot = { owner: null, entries: [], lastMessageAt: null };
let store: Snapshot = EMPTY;
let gen = 0;
let seq = 0; // monotonic for the module lifetime: ids never repeat
const listeners = new Set<() => void>();

const emit = () => listeners.forEach((l) => l());

export function subscribe(l: () => void): () => void {
  listeners.add(l);
  return () => void listeners.delete(l);
}

/** Pure: another owner (or another org of the same user) sees nothing. */
export function getSnapshot(owner: string | null): Snapshot {
  return owner !== null && store.owner === owner ? store : EMPTY;
}

export const getServerSnapshot = (): Snapshot => EMPTY;
export const generation = () => gen;
export const currentSeq = () => seq;
export const nextId = () => `e${++seq}`;
export const idNum = (id: string) => Number(id.slice(1));

export function isExpired(now = Date.now()): boolean {
  return store.lastMessageAt !== null && now - store.lastMessageAt > IDLE_MS;
}

// ── storage (every access guarded; a throwing storage just means no mirror) ──

function removeKey() {
  try {
    window.sessionStorage.removeItem(KEY);
  } catch { /* storage unavailable: the module store still works */ }
}

function persist() {
  if (store.owner === null) return;
  try {
    window.sessionStorage.setItem(KEY, JSON.stringify({
      owner: store.owner, lastMessageAt: store.lastMessageAt, seq, entries: store.entries.slice(-KEEP),
    }));
  } catch { /* quota or blocked: keep going in memory */ }
}

const isObj = (x: unknown): x is Record<string, unknown> => typeof x === "object" && x !== null && !Array.isArray(x);
const isStr = (x: unknown): x is string => typeof x === "string";

function validEntry(x: unknown): x is Entry {
  if (!isObj(x) || !isStr(x.id) || !/^e\d+$/.test(x.id)) return false;
  switch (x.kind) {
    case "user": case "assistant":
      return isStr(x.text);
    case "tool":
      return isStr(x.name);
    case "error":
      return isStr(x.code);
    case "preview": {
      const a = x.action;
      if (!isStr(x.tool) || !isObj(a) || !isStr(a.action_id) || !isStr(a.summary)) return false;
      if (!Array.isArray(a.changes) || !a.changes.every((c) => isObj(c) && isStr(c.entity) && isStr(c.field))) return false;
      if (a.warnings !== undefined && !Array.isArray(a.warnings)) return false;
      if (a.context !== undefined && !isObj(a.context)) return false;
      return x.outcome === undefined || (isStr(x.outcome) && OUTCOMES.includes(x.outcome));
    }
    default:
      return false;
  }
}

function readStored(): { owner: string; lastMessageAt: number | null; seq: number; entries: Entry[] } | null {
  try {
    const raw = window.sessionStorage.getItem(KEY);
    if (raw === null) return null;
    const p: unknown = JSON.parse(raw);
    if (!isObj(p) || !isStr(p.owner) || !Array.isArray(p.entries)) return null;
    return {
      owner: p.owner,
      lastMessageAt: typeof p.lastMessageAt === "number" ? p.lastMessageAt : null,
      seq: typeof p.seq === "number" ? p.seq : 0,
      entries: p.entries.filter(validEntry),
    };
  } catch {
    return null;
  }
}

// ── the four mutators ──

/** The only owner switch and the only storage read. Idempotent per owner. */
export function adoptOwner(owner: string) {
  if (store.owner === owner) return;
  if (store.owner !== null) gen++;
  store = { owner, entries: [], lastMessageAt: null };
  const s = readStored();
  if (s) {
    if (s.owner !== owner || (s.lastMessageAt !== null && Date.now() - s.lastMessageAt > IDLE_MS)) {
      removeKey();
    } else {
      store = { owner, entries: s.entries, lastMessageAt: s.lastMessageAt };
      seq = Math.max(seq, s.seq);
    }
  } else {
    removeKey(); // malformed JSON reads as empty and is dropped
  }
  emit();
}

/** Sign-out only: the store becomes ownerless and drops every write. */
export function clearAssistantSession() {
  store = EMPTY;
  removeKey();
  gen++;
  emit();
}

/** New chat and idle expiry: keeps the owner so the next send persists. */
export function resetConversation() {
  store = { owner: store.owner, entries: [], lastMessageAt: null };
  removeKey();
  gen++;
  emit();
}

/** Every entry write. Dropped when ownerless or when `g` is no longer current. */
export function updateEntries(g: number, fn: (prev: Entry[]) => Entry[], userMessage = false) {
  if (store.owner === null || g !== gen) return;
  const next = fn(store.entries);
  if (next === store.entries && !userMessage) return;
  store = { ...store, entries: next, lastMessageAt: userMessage ? Date.now() : store.lastMessageAt };
  persist();
  emit();
}

// ── rehydration (server decides every label) ──

function label(row: AgentActionRow | undefined): string | null {
  if (row?.live) return null;
  if (!row) return "decided";
  if (row.status === "done") return "done";
  if (row.status === "cancelled") return "cancelled";
  return row.status === "pending" ? "expired" : "decided";
}

export function mergeServerActions(g: number, seqAtSend: number, rows: AgentActionRow[]) {
  updateEntries(g, (prev) => {
    const byId = new Map(rows.map((r) => [r.action_id, r]));
    let changed = false;
    const next = prev.map((e) => {
      if (e.kind !== "preview" || e.outcome || idNum(e.id) > seqAtSend) return e;
      const o = label(byId.get(e.action.action_id));
      if (!o) return e;
      changed = true;
      return { ...e, outcome: o };
    });
    const held = new Set(next.flatMap((e) => (e.kind === "preview" ? [e.action.action_id] : [])));
    const added: Entry[] = rows
      .filter((r) => r.live && !r.preview.context?.reverts && !held.has(r.action_id))
      .sort((a, b) => a.created_at.localeCompare(b.created_at))
      .map((r) => ({
        kind: "preview" as const, id: nextId(), tool: r.tool,
        action: { action_id: r.action_id, ...r.preview, expires_at: r.expires_at, requires_confirmation: true },
      }));
    return changed || added.length ? [...next, ...added] : prev;
  });
}
