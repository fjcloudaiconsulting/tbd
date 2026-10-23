/**
 * TBD-614 store-level fences: F4, F7 (ownerless write), F9 (storage half),
 * F15 (seq across modules), F19c. The component fences live in
 * tests/components/agent/assistant-chat-persistence.test.tsx.
 */
import {
  adoptOwner, clearAssistantSession, generation, getSnapshot, nextId, resetConversation,
  subscribe, updateEntries,
} from "@/lib/agent/session";
import type { Entry } from "@/lib/agent/chat";

const KEY = "tbd-assistant:v1";
const A = "5:7";
const user = (id: string, text = "hi"): Entry => ({ kind: "user", id, text });
const card = (id: string, extra: object = {}): Entry => ({
  kind: "preview", id, tool: "budgets_update_amount",
  action: {
    action_id: `a-${id}`, summary: "s", changes: [{ entity: "budgets", id: 1, field: "amount", before: "1", after: "2" }],
    warnings: [], context: {}, expires_at: "2026-10-09T12:10:00", requires_confirmation: true,
  },
  ...extra,
});
const put = (v: unknown) => window.sessionStorage.setItem(KEY, typeof v === "string" ? v : JSON.stringify(v));
const stored = (over: object = {}) => ({ owner: A, lastMessageAt: Date.now(), seq: 3, entries: [user("e1")], ...over });

beforeEach(() => {
  clearAssistantSession();
  window.sessionStorage.clear();
});

describe("F4: getSnapshot is pure", () => {
  it("another user, or the same user in another org, sees nothing and nothing is touched", () => {
    adoptOwner(A);
    updateEntries(generation(), () => [user("e1")], true);
    const before = window.sessionStorage.getItem(KEY);
    expect(before).not.toBeNull();
    const heard = vi.fn();
    const off = subscribe(heard);

    expect(getSnapshot("9:11").entries).toEqual([]);
    expect(getSnapshot("5:8").entries).toEqual([]);
    expect(getSnapshot(null).entries).toEqual([]);

    expect(window.sessionStorage.getItem(KEY)).toBe(before);
    expect(getSnapshot(A).entries).toHaveLength(1);
    expect(heard).not.toHaveBeenCalled();
    off();
  });
});

describe("F7: an ownerless store drops writes", () => {
  it("a direct entry write after sign-out changes nothing", () => {
    adoptOwner(A);
    clearAssistantSession();
    const g = generation();
    updateEntries(g, () => [user("e1")], true);
    expect(window.sessionStorage.getItem(KEY)).toBeNull();
    expect(getSnapshot(A).entries).toEqual([]);
  });

  it("a write from before an owner switch, reset or sign-out is dropped by the generation", () => {
    adoptOwner(A);
    const g = generation();
    resetConversation();
    updateEntries(g, () => [user("e1")], true);
    expect(getSnapshot(A).entries).toEqual([]);
    expect(window.sessionStorage.getItem(KEY)).toBeNull();
  });
});

describe("F9: idle window and bad storage", () => {
  it("a stored lastMessageAt 2h+1m old is not hydrated and the key is removed", () => {
    put(stored({ lastMessageAt: Date.now() - (2 * 60 + 1) * 60_000 }));
    adoptOwner(A);
    expect(getSnapshot(A).entries).toEqual([]);
    expect(window.sessionStorage.getItem(KEY)).toBeNull();
  });

  it("a stored lastMessageAt just inside the window is hydrated", () => {
    put(stored({ lastMessageAt: Date.now() - 119 * 60_000 }));
    adoptOwner(A);
    expect(getSnapshot(A).entries).toHaveLength(1);
  });

  it("another owner's key is removed on adopt", () => {
    put(stored({ owner: "9:11" }));
    adoptOwner(A);
    expect(getSnapshot(A).entries).toEqual([]);
    expect(window.sessionStorage.getItem(KEY)).toBeNull();
  });

  it("malformed JSON is dropped without throwing", () => {
    put("{not json");
    expect(() => adoptOwner(A)).not.toThrow();
    expect(getSnapshot(A).entries).toEqual([]);
    expect(window.sessionStorage.getItem(KEY)).toBeNull();
  });

  it.each([
    ["a non-array changes", card("e2", { action: { ...(card("e2") as Extract<Entry, { kind: "preview" }>).action, changes: "x" } })],
    ["a changes item without a string entity", card("e2", { action: { ...(card("e2") as Extract<Entry, { kind: "preview" }>).action, changes: [{ field: "amount" }] } })],
    ["a changes item without a string field", card("e2", { action: { ...(card("e2") as Extract<Entry, { kind: "preview" }>).action, changes: [{ entity: "budgets" }] } })],
    ["an unknown outcome", card("e2", { outcome: "exploded" })],
    ["non-array warnings", card("e2", { action: { ...(card("e2") as Extract<Entry, { kind: "preview" }>).action, warnings: "w" } })],
    ["an unknown kind", { kind: "mystery", id: "e2" }],
    ["a non-string text", { kind: "user", id: "e2", text: 5 }],
  ])("an entry with %s is dropped; the valid ones survive", (_n, bad) => {
    put(stored({ entries: [user("e1"), bad, card("e3")] }));
    expect(() => adoptOwner(A)).not.toThrow();
    expect(getSnapshot(A).entries.map((e) => e.id)).toEqual(["e1", "e3"]);
  });

  it("a throwing sessionStorage falls back to memory", () => {
    const get = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    const rm = vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => { throw new Error("blocked"); });
    try {
      expect(() => adoptOwner(A)).not.toThrow();
      expect(() => updateEntries(generation(), () => [user("e1")], true)).not.toThrow();
      expect(getSnapshot(A).entries).toHaveLength(1);
      expect(() => resetConversation()).not.toThrow();
    } finally {
      get.mockRestore(); set.mockRestore(); rm.mockRestore();
    }
  });
});

describe("F15: seq is monotonic across modules and resets", () => {
  it("a fresh module hydrating a stored seq of 12 issues e13 next", async () => {
    vi.resetModules();
    const fresh = await import("@/lib/agent/session");
    put(stored({ seq: 12, entries: [user("e9"), user("e12")] }));
    fresh.adoptOwner(A);
    expect(fresh.nextId()).toBe("e13");
  });

  it("adopting never lowers the module's own seq (max, not assignment)", async () => {
    vi.resetModules();
    const fresh = await import("@/lib/agent/session");
    for (let i = 0; i < 20; i++) fresh.nextId();
    put(stored({ seq: 12 }));
    fresh.adoptOwner(A);
    expect(fresh.nextId()).toBe("e21");
  });

  it("clear, reset and an owner switch never rewind it", () => {
    adoptOwner(A);
    const n = Number(nextId().slice(1));
    resetConversation();
    clearAssistantSession();
    adoptOwner("9:11");
    expect(Number(nextId().slice(1))).toBe(n + 1);
  });
});

describe("F19c: a same-owner entry older than 2h is removed on adopt", () => {
  it("removes the key and leaves an empty store", () => {
    put(stored({ lastMessageAt: Date.now() - 3 * 3600_000 }));
    adoptOwner(A);
    expect(window.sessionStorage.getItem(KEY)).toBeNull();
    expect(getSnapshot(A).entries).toEqual([]);
  });
});

describe("D2.4: every mutator emits", () => {
  it("adopt that hydrates, reset and clear each notify subscribers", () => {
    const heard = vi.fn();
    const off = subscribe(heard);
    put(stored());
    adoptOwner(A);
    expect(heard).toHaveBeenCalledTimes(1);
    adoptOwner(A); // idempotent: no emit, no generation bump
    expect(heard).toHaveBeenCalledTimes(1);
    resetConversation();
    expect(heard).toHaveBeenCalledTimes(2);
    clearAssistantSession();
    expect(heard).toHaveBeenCalledTimes(3);
    off();
  });
});
