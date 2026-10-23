/**
 * TBD-614 the assistant chat survives navigation. Fences F1-F3, F7, F9-F15,
 * F17, F18, F21-F24 and guards G1-G3 (spec rev 6.1, local). F4/F7b/F15b/F19c
 * are store-level (tests/lib/agent/session.test.ts); F5/F6/F19/F20 drive the
 * real AuthProvider (assistant-session-auth.test.tsx).
 *
 * The auth context is mocked here; the transport is the real apiFetch over a
 * routed global fetch.
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { mutate } from "swr";

import AssistantChat from "@/components/agent/AssistantChat";
import { setAccessToken, signInWithAccessToken } from "@/lib/api";
import type { Entry } from "@/lib/agent/chat";
import {
  adoptOwner, clearAssistantSession, getSnapshot, resetConversation, updateEntries, generation,
} from "@/lib/agent/session";
import type { AgentActionRow, StagedAction } from "@/lib/agent/types";

const auth = vi.hoisted(() => ({ user: { id: 1, org_id: 1 } as { id: number; org_id: number } | null }));
vi.mock("@/components/auth/AuthProvider", () => ({ useAuth: () => ({ user: auth.user }) }));

const KEY = "tbd-assistant:v1";
const OWNER = "1:1";
const enc = new TextEncoder();

type Res = Response | Promise<Response>;
type Ev = [string, unknown];

const body = (events: Ev[]) => events.map(([e, d]) => `event: ${e}\ndata: ${JSON.stringify(d)}\n\n`).join("");

function sse(events: Ev[], { done = true } = {}): Response {
  return new Response(
    new ReadableStream({
      start(c) {
        c.enqueue(enc.encode(body(events) + (done ? "event: done\ndata: {}\n\n" : "")));
        c.close();
      },
    }),
    { status: 200, headers: { "content-type": "text/event-stream" } },
  );
}

/** A stream that emits `first` now and `later` (then done) once released. */
function heldStream(first: Ev[], later: Ev[] = []) {
  let release!: () => void;
  const gate = new Promise<void>((r) => { release = r; });
  const res = new Response(
    new ReadableStream({
      async start(c) {
        c.enqueue(enc.encode(body(first)));
        await gate;
        try {
          c.enqueue(enc.encode(body(later) + "event: done\ndata: {}\n\n"));
          c.close();
        } catch { /* cancelled while held */ }
      },
    }),
    { status: 200, headers: { "content-type": "text/event-stream" } },
  );
  return { res, release };
}

const json = (status: number, b: unknown) =>
  new Response(JSON.stringify(b), { status, headers: { "content-type": "application/json" } });

function deferred() {
  let resolve!: (r: Response) => void;
  const p = new Promise<Response>((r) => { resolve = r; });
  return { p, resolve };
}

const BUDGET_ACTION: StagedAction = {
  action_id: "a1",
  summary: "Change the amount of budget 12 from 400.00 to 1234.00 EUR",
  changes: [{ entity: "budgets", id: 12, field: "amount", before: "400.00", after: "1234.00", currency: "EUR" }],
  warnings: [],
  context: { category_name: { untrusted: "Groceries" }, period_start: "2026-10-01" },
  expires_at: "2026-10-09T12:10:00",
  requires_confirmation: true,
};

const row = (o: Partial<AgentActionRow> & { action_id: string }): AgentActionRow => ({
  tool: "budgets_update_amount", channel: "in_app", risk: "write", mode: "confirm", status: "pending",
  preview: {
    summary: BUDGET_ACTION.summary, changes: BUDGET_ACTION.changes, warnings: [], context: BUDGET_ACTION.context,
  },
  error_code: null, created_at: "2026-10-09T12:00:00", expires_at: "2026-10-09T12:10:00",
  decided_at: null, live: true, ...o,
});

const pv = (n: number, actionId: string, extra: object = {}): Entry => ({
  kind: "preview", id: `e${n}`, tool: "budgets_update_amount", action: { ...BUDGET_ACTION, action_id: actionId }, ...extra,
});

let fetchMock: ReturnType<typeof vi.fn>;
let actions: () => Res;
let chats: Array<() => Res>;
let decide: (url: string) => Res;

beforeEach(() => {
  auth.user = { id: 1, org_id: 1 };
  setAccessToken("test-token");
  actions = () => json(200, { items: [] });
  chats = [];
  decide = () => json(200, { status: "done" });
  fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/api/v1/agent/actions?")) return actions();
    if (url.endsWith("/api/v1/agent/chat")) return chats.shift()!();
    if (/\/(confirm|cancel)$/.test(url)) return decide(url);
    throw new Error(`unexpected fetch ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  clearAssistantSession();
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  signInWithAccessToken(null);
});

/** Hydrates through the real storage path: the module store is cleared first. */
function seed(entries: Entry[], o: { lastMessageAt?: number | null } = {}) {
  clearAssistantSession();
  const seq = Math.max(0, ...entries.map((e) => Number(e.id.slice(1))));
  window.sessionStorage.setItem(KEY, JSON.stringify({ owner: OWNER, lastMessageAt: o.lastMessageAt ?? null, seq, entries }));
}

const input = () => screen.getByLabelText("Message the assistant") as HTMLTextAreaElement;
const send = (text: string) => {
  fireEvent.change(input(), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
};
async function ask(text: string) {
  send(text);
  await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument());
}
const chatBodies = () =>
  fetchMock.mock.calls
    .filter(([u]) => String(u).endsWith("/api/v1/agent/chat"))
    .map(([, init]) => JSON.parse((init as RequestInit).body as string));
const confirms = () => fetchMock.mock.calls.map(([u]) => String(u)).filter((u) => u.endsWith("/confirm"));
const cards = () => screen.queryAllByTestId("preview-card");
const stored = () => JSON.parse(window.sessionStorage.getItem(KEY) ?? "null");
const stage = (action = BUDGET_ACTION) =>
  sse([["message", { text: "Here you go." }], ["tool_call", { name: "budgets_update_amount" }], ["preview", { action }]]);
const apply = () => screen.queryByRole("button", { name: "Apply change" });
const idsOf = () => getSnapshot(OWNER).entries.map((e) => e.id);

describe("F1-F3, F24: history survives leaving the page", () => {
  it("F1: after unmount and a cleared session the server row renders with Apply and Apply posts its id", async () => {
    chats.push(() => stage());
    const { unmount } = render(<AssistantChat />);
    await ask("raise groceries");
    expect(apply()).toBeInTheDocument();
    unmount();
    clearAssistantSession(); // nothing left in memory or storage

    actions = () => json(200, { items: [row({ action_id: "a1" })] });
    render(<AssistantChat />);
    const btn = await screen.findByRole("button", { name: "Apply change" });
    fireEvent.click(btn);
    await waitFor(() => expect(screen.getByText("Applied")).toBeInTheDocument());
    expect(confirms()).toEqual(["/api/v1/agent/actions/a1/confirm"]);
  });

  it("F2: the transcript is still there after an unmount and remount", async () => {
    chats.push(() => sse([["message", { text: "It is 42." }]]));
    const { unmount } = render(<AssistantChat />);
    await ask("what is the answer");
    unmount();
    render(<AssistantChat />);
    expect(await screen.findByText("what is the answer")).toBeInTheDocument();
    expect(screen.getByTestId("assistant-text").textContent).toBe("It is 42.");
  });

  it("F3: leave mid-reply, return, send: the orphan is marked failed and not merged into the next body", async () => {
    const held = heldStream([["tool_call", { name: "budgets_list" }]]);
    chats.push(() => held.res);
    const { unmount } = render(<AssistantChat />);
    send("first");
    await waitFor(() => expect(screen.getByRole("button", { name: "Stop response" })).toBeInTheDocument());
    await act(async () => { await new Promise((r) => setTimeout(r, 20)); });
    unmount();
    await waitFor(() =>
      expect(getSnapshot(OWNER).entries.find((e) => e.kind === "user")).toMatchObject({ failed: true }),
    );

    chats.push(() => sse([["message", { text: "ok" }]]));
    render(<AssistantChat />);
    await ask("second");
    expect(chatBodies().at(-1).messages).toEqual([{ role: "user", content: "second" }]);
  });

  it("F24: a fresh module hydrating a stored history shows it with no further write", async () => {
    seed([{ kind: "user", id: "e1", text: "remember me" }, { kind: "assistant", id: "e2", text: "I do." }]);
    render(<AssistantChat />); // GET returns []
    expect(await screen.findByText("remember me")).toBeInTheDocument();
    expect(screen.getByText("I do.")).toBeInTheDocument();
  });
});

describe("F7: sign-out while a turn streams", () => {
  it("later events re-create neither the key nor the entries", async () => {
    const held = heldStream([["message", { text: "part one" }]], [["message", { text: "part two" }], ["tool_call", { name: "budgets_list" }]]);
    chats.push(() => held.res);
    render(<AssistantChat />);
    send("hi");
    await screen.findByText("part one");
    act(() => clearAssistantSession());
    await act(async () => { held.release(); });
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument());
    expect(window.sessionStorage.getItem(KEY)).toBeNull();
    expect(getSnapshot(OWNER).entries).toEqual([]);
  });
});

describe("F9: the idle window, through the component", () => {
  const T0 = new Date("2026-10-20T10:00:00Z");

  it("a store whose last user message is 2h+1m old is empty after remount, and a send persists under the same owner", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(T0);
    chats.push(() => sse([["message", { text: "hello there" }]]));
    const { unmount } = render(<AssistantChat />);
    await ask("hi");
    unmount();
    vi.setSystemTime(new Date(T0.getTime() + (2 * 60 + 1) * 60_000));

    chats.push(() => sse([["message", { text: "again" }]]));
    render(<AssistantChat />);
    await waitFor(() => expect(getSnapshot(OWNER).entries).toEqual([]));
    expect(screen.queryByText("hello there")).toBeNull();
    await ask("later");
    expect(stored().owner).toBe(OWNER);
    expect(chatBodies().at(-1).messages).toEqual([{ role: "user", content: "later" }]);
  });

  it("a newer assistant entry and a newer card decision do not extend it", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(T0);
    chats.push(() => stage());
    const { unmount } = render(<AssistantChat />);
    await ask("go");
    vi.setSystemTime(new Date(T0.getTime() + 110 * 60_000)); // 1h50 in: decide the card
    fireEvent.click(screen.getByRole("button", { name: "Apply change" }));
    await screen.findByText("Applied");
    unmount();
    vi.setSystemTime(new Date(T0.getTime() + (2 * 60 + 1) * 60_000));

    render(<AssistantChat />);
    await waitFor(() => expect(getSnapshot(OWNER).entries).toEqual([]));
    expect(cards()).toHaveLength(0);
  });

  it("a conversation just inside the window is kept", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(T0);
    chats.push(() => sse([["message", { text: "kept" }]]));
    const { unmount } = render(<AssistantChat />);
    await ask("hi");
    unmount();
    vi.setSystemTime(new Date(T0.getTime() + 119 * 60_000));
    render(<AssistantChat />);
    expect(await screen.findByText("kept")).toBeInTheDocument();
  });
});

describe("F10-F14: the server decides every label", () => {
  it("F10: live / done / cancelled / pending-not-live / failed / absent render Apply / Applied / Discarded / Expired+sentence / Already decided x2, with the client clock 15 min fast", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date("2026-10-09T12:25:00Z")); // 15 min past the stored expires_at
    seed([pv(1, "x1"), pv(2, "x2"), pv(3, "x3"), pv(4, "x4"), pv(5, "x5"), pv(6, "x6")]);
    actions = () => json(200, {
      items: [
        row({ action_id: "x1" }),
        row({ action_id: "x2", status: "done", live: false }),
        row({ action_id: "x3", status: "cancelled", live: false }),
        row({ action_id: "x4", status: "pending", live: false }),
        row({ action_id: "x5", status: "failed" as never, live: false }),
      ],
    });
    render(<AssistantChat />);
    await waitFor(() => expect(cards()).toHaveLength(6));
    const [c1, c2, c3, c4, c5, c6] = cards();
    expect(within(c1).getByRole("button", { name: "Apply change" })).toBeInTheDocument();
    expect(within(c2).getByText("Applied")).toBeInTheDocument();
    expect(within(c3).getByText("Discarded")).toBeInTheDocument();
    expect(within(c4).getByText("Expired")).toBeInTheDocument();
    expect(within(c4).getByText("This change expired before it was applied. Ask again for a fresh one.")).toBeInTheDocument();
    expect(within(c5).getByText("Already decided")).toBeInTheDocument();
    expect(within(c6).getByText("Already decided")).toBeInTheDocument();
    for (const c of [c2, c3, c4, c5, c6]) expect(within(c).queryByRole("button", { name: "Apply change" })).toBeNull();
    expect(screen.queryByText("This change expired before it was applied. Ask again for a fresh one.", { selector: "p" })
      ?.closest("[data-testid=preview-card]")).toBe(c4);
  });

  it("F11: an unreferenced live Activity revert is not appended; an unreferenced live chat row is", async () => {
    actions = () => json(200, {
      items: [
        row({ action_id: "rv", preview: { ...row({ action_id: "rv" }).preview, context: { reverts: "orig" } } }),
        row({ action_id: "chatrow" }),
      ],
    });
    render(<AssistantChat />);
    await waitFor(() => expect(cards()).toHaveLength(1));
    expect(getSnapshot(OWNER).entries.map((e) => (e.kind === "preview" ? e.action.action_id : ""))).toEqual(["chatrow"]);
  });

  it("F12: a stored undecided card is not rendered before the GET resolves", async () => {
    seed([pv(1, "x1")]);
    const d = deferred();
    actions = () => d.p;
    render(<AssistantChat />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await act(async () => {});
    expect(cards()).toHaveLength(0);
    await act(async () => d.resolve(json(200, { items: [row({ action_id: "x1", status: "done", live: false })] })));
    await waitFor(() => expect(cards()).toHaveLength(1));
    expect(screen.getByText("Applied")).toBeInTheDocument();
    expect(apply()).toBeNull(); // never mounted as confirmable, then contradicted
  });

  it("F12: when the GET fails the stored card renders unchanged", async () => {
    seed([pv(1, "x1")]);
    actions = () => json(500, { detail: "boom" });
    render(<AssistantChat />);
    await waitFor(() => expect(cards()).toHaveLength(1));
    expect(apply()).toBeInTheDocument();
    expect(screen.queryByText("Already decided")).toBeNull();
  });

  it("F12: the GET is not served from a pre-seeded SWR cache", async () => {
    await mutate("/api/v1/agent/actions?channel=in_app&limit=50", { items: [row({ action_id: "x1" })] }, false);
    seed([pv(1, "x1")]);
    actions = () => json(200, { items: [row({ action_id: "x1", status: "done", live: false })] });
    render(<AssistantChat />);
    expect(await screen.findByText("Applied")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([u]) => String(u) === "/api/v1/agent/actions?channel=in_app&limit=50")).toBe(true);
  });

  it("F13: a restored outcome done shows Applied and no buttons", async () => {
    seed([pv(1, "x1", { outcome: "done" })]);
    render(<AssistantChat />);
    expect(await screen.findByText("Applied")).toBeInTheDocument();
    expect(apply()).toBeNull();
    expect(screen.queryByRole("button", { name: "Discard" })).toBeNull();
  });

  describe("F14: a stale swap is remembered", () => {
    const fresh = { ...BUDGET_ACTION, action_id: "a2", changes: [{ ...BUDGET_ACTION.changes[0], before: "500.00" }] };

    async function stageAndSwap() {
      chats.push(() => stage());
      decide = () => json(409, { detail: { code: "preview_stale", message: "changed", ...fresh } });
      const view = render(<AssistantChat />);
      await ask("go");
      fireEvent.click(screen.getByRole("button", { name: "Apply change" }));
      await waitFor(() => expect(screen.getByTestId("preview-card").textContent).toMatch(/€500\.00/));
      view.unmount();
    }

    it.each([
      ["the GET fails", () => json(500, {})],
      ["the GET lists the fresh row", () => json(200, { items: [row({ action_id: "a2", preview: { ...row({ action_id: "a2" }).preview, changes: fresh.changes } })] })],
    ])("one card with the FRESH diff and id after remount when %s", async (_n, get) => {
      await stageAndSwap();
      actions = get;
      render(<AssistantChat />);
      await waitFor(() => expect(cards()).toHaveLength(1));
      expect(cards()[0].textContent).toMatch(/€500\.00/);
      expect(cards()[0].textContent).not.toMatch(/€400\.00/);
      decide = () => json(200, { status: "done" });
      await act(async () => { await new Promise((r) => setTimeout(r, 800)); }); // the swap re-arm window passed with the old instance
      fireEvent.click(screen.getByRole("button", { name: "Apply change" }));
      await screen.findByText("Applied");
      expect(confirms().at(-1)).toBe("/api/v1/agent/actions/a2/confirm");
    });
  });
});

describe("F15: ids", () => {
  it("restored ids plus a new turn stay unique; deciding the new card leaves the restored ones alone; a new card is never relabelled by an older GET", async () => {
    seed([pv(9, "x9"), pv(12, "x12")]);
    const d = deferred();
    actions = () => d.p;
    chats.push(() => stage({ ...BUDGET_ACTION, action_id: "fresh" }));
    render(<AssistantChat />);
    await ask("go"); // the new card arrives while the mount GET is still out
    await act(async () => d.resolve(json(200, { items: [row({ action_id: "x9" }), row({ action_id: "x12" })] })));
    await waitFor(() => expect(cards()).toHaveLength(3));

    const ids = idsOf();
    expect(new Set(ids).size).toBe(ids.length);
    const nums = ids.map((i) => Number(i.slice(1)));
    expect(Math.min(...nums.slice(2))).toBeGreaterThan(12);
    expect(screen.queryByText("Already decided")).toBeNull();

    const fresh = cards().find((c) => c.getAttribute("aria-labelledby") === "preview-fresh")!;
    fireEvent.click(within(fresh).getByRole("button", { name: "Apply change" }));
    await within(fresh).findByText("Applied");
    const after = getSnapshot(OWNER).entries.filter((e) => e.kind === "preview");
    expect(after.filter((e) => e.kind === "preview" && e.outcome).map((e) => e.kind === "preview" && e.action.action_id)).toEqual(["fresh"]);
  });
});

describe("F17-F18, F22, F23, G2: New chat", () => {
  async function chatted() {
    chats.push(() => stage());
    const view = render(<AssistantChat />);
    await ask("go");
    return view;
  }

  it("F17: empties the entries and the key, keeps the owner, and the next send is clean, persisted and has a larger id", async () => {
    chats.push(() => sse([["message", { text: "one" }]]));
    render(<AssistantChat />);
    await ask("first");
    const before = Math.max(...idsOf().map((i) => Number(i.slice(1))));
    fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    expect(getSnapshot(OWNER).entries).toEqual([]);
    expect(getSnapshot(OWNER).owner).toBe(OWNER);
    expect(window.sessionStorage.getItem(KEY)).toBeNull();

    chats.push(() => sse([["message", { text: "two" }]]));
    await ask("second");
    expect(chatBodies().at(-1).messages).toEqual([{ role: "user", content: "second" }]);
    expect(stored().owner).toBe(OWNER);
    expect(Math.min(...idsOf().map((i) => Number(i.slice(1))))).toBeGreaterThan(before);
  });

  it("F18: a live pending row comes back as a confirmable card after New chat", async () => {
    await chatted();
    actions = () => json(200, { items: [row({ action_id: "a1" })] });
    fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    await waitFor(() => expect(apply()).toBeInTheDocument());
    expect(cards()).toHaveLength(1);
    fireEvent.click(apply()!);
    await screen.findByText("Applied");
    expect(confirms()).toEqual(["/api/v1/agent/actions/a1/confirm"]);
  });

  it("F22: a held GET resolving after sign-out leaves no key and no card", async () => {
    const d = deferred();
    actions = () => d.p;
    render(<AssistantChat />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    act(() => clearAssistantSession());
    await act(async () => d.resolve(json(200, { items: [row({ action_id: "a1" })] })));
    expect(window.sessionStorage.getItem(KEY)).toBeNull();
    expect(cards()).toHaveLength(0);
  });

  it("F22: a held GET for owner A resolving after an A-to-B switch lands nothing in B's store", async () => {
    const d = deferred();
    actions = () => d.p;
    const view = render(<AssistantChat />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    actions = () => json(200, { items: [] });
    auth.user = { id: 2, org_id: 2 };
    act(() => adoptOwner("2:2"));
    view.rerender(<AssistantChat />);
    await act(async () => d.resolve(json(200, { items: [row({ action_id: "a1" })] })));
    expect(getSnapshot("2:2").entries).toEqual([]);
    expect(window.sessionStorage.getItem(KEY)).toBeNull();
  });

  it("F22: a held mount GET resolving after New chat is ignored; only the New chat GET's result is merged", async () => {
    seed([{ kind: "user", id: "e1", text: "old" }, { kind: "assistant", id: "e2", text: "reply" }]);
    const d1 = deferred();
    const d2 = deferred();
    const queue = [d1, d2];
    actions = () => queue.shift()!.p;
    render(<AssistantChat />);
    const newChat = await screen.findByRole("button", { name: "New chat" });
    fireEvent.click(newChat);
    await act(async () => d2.resolve(json(200, { items: [row({ action_id: "second" })] })));
    await act(async () => d1.resolve(json(200, { items: [row({ action_id: "first" })] })));
    await waitFor(() => expect(cards()).toHaveLength(1));
    expect(cards()[0].getAttribute("aria-labelledby")).toBe("preview-second");
  });

  it("F23: after New chat empties the transcript the textarea has focus; restored and GET-appended cards never take it; an SSE card does", async () => {
    seed([{ kind: "user", id: "e1", text: "old" }, { kind: "assistant", id: "e2", text: "reply" }, pv(3, "x3")]);
    actions = () => json(200, { items: [row({ action_id: "x3" }), row({ action_id: "appended" })] });
    render(<AssistantChat />);
    await waitFor(() => expect(cards()).toHaveLength(2));
    for (const c of cards()) expect(document.activeElement).not.toBe(c);

    fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    expect(document.activeElement).toBe(input());
    await waitFor(() => expect(cards()).toHaveLength(2)); // the GET brought them back
    expect(document.activeElement).toBe(input());

    chats.push(() => stage({ ...BUDGET_ACTION, action_id: "live-sse" }));
    await ask("go");
    await waitFor(() => expect(document.activeElement).toBe(cards().find((c) => c.getAttribute("aria-labelledby") === "preview-live-sse")));
  });

  it("G2: the button is absent on an empty transcript", async () => {
    render(<AssistantChat />);
    await act(async () => {});
    expect(screen.queryByRole("button", { name: "New chat" })).toBeNull();
  });

  it("G2: the button is absent when the transcript holds only undecided live cards", async () => {
    seed([pv(1, "a1")]);
    actions = () => json(200, { items: [row({ action_id: "a1" })] });
    render(<AssistantChat />);
    await waitFor(() => expect(cards()).toHaveLength(1));
    expect(screen.queryByRole("button", { name: "New chat" })).toBeNull();
  });

  it("G2: it is aria-disabled while streaming, a click then changes nothing, and a typed draft survives New chat", async () => {
    chats.push(() => sse([["message", { text: "one" }]]));
    render(<AssistantChat />);
    await ask("first");
    fireEvent.change(input(), { target: { value: "my draft" } });
    fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    expect(input().value).toBe("my draft");

    const held = heldStream([["message", { text: "partial" }]]);
    chats.push(() => held.res);
    send("second");
    await screen.findByText("partial");
    const btn = screen.getByRole("button", { name: "New chat" });
    expect(btn).toHaveAttribute("aria-disabled", "true");
    const keyBefore = window.sessionStorage.getItem(KEY);
    const idsBefore = idsOf();
    fireEvent.click(btn);
    expect(idsOf()).toEqual(idsBefore);
    expect(window.sessionStorage.getItem(KEY)).toBe(keyBefore);
    expect(screen.getByRole("button", { name: "Stop response" })).toBeInTheDocument();
    await act(async () => held.release());
  });
});

describe("F21: a card staged while the mount GET is out is never contradicted", () => {
  const X = { ...BUDGET_ACTION, action_id: "x" };

  it("(a) the SSE preview arrives before the GET resolves with X live", async () => {
    const d = deferred();
    actions = () => d.p;
    chats.push(() => stage(X));
    render(<AssistantChat />);
    await ask("go");
    expect(cards()).toHaveLength(1);
    await act(async () => d.resolve(json(200, { items: [row({ action_id: "x" })] })));
    expect(cards()).toHaveLength(1);
    expect(apply()).toBeInTheDocument();
    expect(screen.queryByText("Already decided")).toBeNull();
  });

  it("(b) the GET resolves with X live (appended) before the SSE preview arrives", async () => {
    const d = deferred();
    actions = () => d.p;
    const held = heldStream([["tool_call", { name: "budgets_update_amount" }]], [["preview", { action: X }]]);
    chats.push(() => held.res);
    render(<AssistantChat />);
    send("go");
    await waitFor(() => expect(screen.getByRole("button", { name: "Stop response" })).toBeInTheDocument());
    await act(async () => d.resolve(json(200, { items: [row({ action_id: "x" })] })));
    await waitFor(() => expect(cards()).toHaveLength(1));
    await act(async () => held.release());
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument());
    expect(cards()).toHaveLength(1);
    expect(apply()).toBeInTheDocument();
    expect(screen.queryByText("Already decided")).toBeNull();
  });

  it("(c) the GET was sent before X was staged and resolves WITHOUT X after its SSE preview", async () => {
    const d = deferred();
    actions = () => d.p;
    chats.push(() => stage(X));
    render(<AssistantChat />);
    await ask("go");
    await act(async () => d.resolve(json(200, { items: [] })));
    expect(cards()).toHaveLength(1);
    expect(apply()).toBeInTheDocument();
    expect(screen.queryByText("Already decided")).toBeNull();
  });
});

describe("F16: a card decision resolving after the session moved writes nothing", () => {
  it.each([
    ["clearAssistantSession", () => clearAssistantSession()],
    ["resetConversation", () => resetConversation()],
  ])("after %s", async (_n, move) => {
    chats.push(() => stage());
    const d = deferred();
    render(<AssistantChat />);
    await ask("go");
    decide = () => d.p;
    fireEvent.click(screen.getByRole("button", { name: "Apply change" }));
    await waitFor(() => expect(confirms()).toHaveLength(1));
    act(() => move());
    updateEntries(generation(), () => [{ kind: "user", id: "e900", text: "fresh" }], true);
    const before = window.sessionStorage.getItem(KEY);
    await act(async () => d.resolve(json(200, { status: "done" })));
    expect(window.sessionStorage.getItem(KEY)).toBe(before);
    expect(getSnapshot(OWNER).entries.some((e) => e.kind === "preview")).toBe(false);
  });
});

describe("G1: guards", () => {
  it("a 410 on confirm shows the expired sentence", async () => {
    chats.push(() => stage());
    render(<AssistantChat />);
    await ask("go");
    decide = () => json(410, { detail: { code: "action_expired", message: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply change" }));
    expect(await screen.findByText("This change expired before it was applied. Ask again for a fresh one.")).toBeInTheDocument();
  });

  it("aria-busy is on while the GET is pending and gone after success and after failure", async () => {
    for (const settle of [() => json(200, { items: [] }), () => json(500, {})]) {
      const d = deferred();
      actions = () => d.p;
      const { unmount } = render(<AssistantChat />);
      expect(screen.getByRole("log")).toHaveAttribute("aria-busy", "true");
      await act(async () => d.resolve(settle()));
      await waitFor(() => expect(screen.getByRole("log")).not.toHaveAttribute("aria-busy"));
      unmount();
    }
  });

  it("while a turn streams during a pending GET, aria-busy is absent", async () => {
    const d = deferred();
    actions = () => d.p;
    const held = heldStream([["message", { text: "partial" }]]);
    chats.push(() => held.res);
    render(<AssistantChat />);
    send("hi");
    await screen.findByText("partial");
    expect(screen.getByRole("log")).not.toHaveAttribute("aria-busy");
    await act(async () => { held.release(); d.resolve(json(200, { items: [] })); });
  });
});

describe("G3: the footer", () => {
  it("states the new retention and keeps the provider sentences", () => {
    render(<AssistantChat />);
    const foot = document.querySelector("form p")!.textContent!;
    expect(foot).toContain(
      "The conversation stays in this tab until you sign out. It starts fresh when you come back after 2 hours without a message.",
    );
    expect(foot).not.toMatch(/close the tab/i);
    expect(foot).not.toContain("—");
    expect(foot).toContain("What you send goes to your organization");
  });
});
