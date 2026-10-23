/**
 * TBD-614 the assistant session follows the signed-in identity. Fences F5, F6,
 * F19 (a-d) and F20, driven through the REAL AuthProvider and the real apiFetch
 * (only the network is mocked). Store-level cases: tests/lib/agent/session.test.ts.
 */
import { useEffect } from "react";
import { act, render, screen, waitFor, fireEvent } from "@testing-library/react";

import AssistantChat from "@/components/agent/AssistantChat";
import { AuthProvider, useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, signInWithAccessToken } from "@/lib/api";
import {
  adoptOwner, clearAssistantSession, generation, getSnapshot, updateEntries,
} from "@/lib/agent/session";

const KEY = "tbd-assistant:v1";
const enc = new TextEncoder();
const b64 = (s: string) => btoa(s).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
const tok = (sub: string, org: number) =>
  `${b64('{"alg":"HS256"}')}.${b64(JSON.stringify({ sub, org_id: org, exp: Math.floor(Date.now() / 1000) + 900 }))}.sig`;
const userA = () => ({ id: 5, org_id: 7, email: "a@x.test" });
const userB = () => ({ id: 9, org_id: 11, email: "b@x.test" });
const A = "5:7";
const B = "9:11";

const json = (status: number, b: unknown) =>
  new Response(JSON.stringify(b), { status, headers: { "content-type": "application/json" } });
const sse = (events: Array<[string, unknown]>) =>
  new Response(
    new ReadableStream({
      start(c) {
        c.enqueue(enc.encode(events.map(([e, d]) => `event: ${e}\ndata: ${JSON.stringify(d)}\n\n`).join("") + "event: done\ndata: {}\n\n"));
        c.close();
      },
    }),
    { status: 200, headers: { "content-type": "text/event-stream" } },
  );
const deferred = () => {
  let resolve!: (r: Response) => void;
  const p = new Promise<Response>((r) => { resolve = r; });
  return { p, resolve };
};

let fetchMock: ReturnType<typeof vi.fn>;
let status: () => Promise<Response> | Response;
let refresh: () => Response;
let me: () => Response;
let chats: Array<() => Promise<Response> | Response>;
let auth!: ReturnType<typeof useAuth>;

beforeEach(() => {
  signInWithAccessToken(null);
  status = () => json(200, { needs_setup: false });
  refresh = () => json(200, { access_token: tok("5", 7) });
  me = () => json(200, userA());
  chats = [];
  fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/api/v1/auth/status")) return status();
    if (url.includes("/api/v1/auth/refresh")) return refresh();
    if (url.includes("/api/v1/auth/me")) return me();
    if (url.includes("/api/v1/auth/login")) return json(200, { access_token: tok("9", 11) });
    if (url.includes("/api/v1/auth/logout")) return json(200, {});
    if (url.includes("/api/v1/agent/actions?")) return json(200, { items: [] });
    if (url.endsWith("/api/v1/agent/chat")) return chats.shift()!();
    return json(401, { detail: "no" });
  });
  vi.stubGlobal("fetch", fetchMock);
  clearAssistantSession();
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  signInWithAccessToken(null);
});

function Harness({ chat = false }: { chat?: boolean }) {
  const ctx = useAuth();
  useEffect(() => { auth = ctx; });
  return (
    <>
      <div data-testid="state">{ctx.loading ? "loading" : ctx.user ? String(ctx.user.id) : "none"}</div>
      {chat && ctx.user ? <AssistantChat key={ctx.user.id} /> : null}
    </>
  );
}
const mount = (chat = false) => render(<AuthProvider><Harness chat={chat} /></AuthProvider>);
const settled = (who: string) => waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent(who));

/** A's populated store and key, as if the assistant had been used. */
function populateAs(owner: string) {
  adoptOwner(owner);
  updateEntries(generation(), () => [{ kind: "user", id: "e1", text: "secret of " + owner }], true);
  expect(window.sessionStorage.getItem(KEY)).not.toBeNull();
}
const wiped = () => {
  expect(window.sessionStorage.getItem(KEY)).toBeNull();
  expect(getSnapshot(A).entries).toEqual([]);
  expect(getSnapshot(B).entries).toEqual([]);
};

describe("F5: every way the user becomes null wipes the session", () => {
  it("manual logout", async () => {
    populateAs(A);
    mount();
    await settled("5");
    expect(getSnapshot(A).entries).toHaveLength(1); // a restore as the same user keeps it
    await act(async () => { await auth.logout(); });
    await settled("none");
    wiped();
  });

  it("a terminal 401 (auth:unauthenticated)", async () => {
    populateAs(A);
    mount();
    await settled("5");
    act(() => { window.dispatchEvent(new Event("auth:unauthenticated")); });
    await settled("none");
    wiped();
  });

  it("a restore failure", async () => {
    populateAs(A);
    refresh = () => json(401, { detail: "no session" });
    mount();
    await settled("none");
    wiped();
  });

  it("a refresh that returns another identity, end to end through apiFetch", async () => {
    populateAs(A);
    mount();
    await settled("5");
    refresh = () => json(200, { access_token: tok("9", 11) });
    await act(async () => { await apiFetch("/api/v1/accounts").catch(() => {}); });
    await settled("none");
    wiped();
  });
});

describe("F6: the initial loading state does not wipe", () => {
  it("a reload keeps the key while the restore is still out, and after it restores the same user", async () => {
    populateAs(A);
    const d = deferred();
    status = () => d.p;
    mount();
    await act(async () => {});
    expect(screen.getByTestId("state")).toHaveTextContent("loading");
    expect(window.sessionStorage.getItem(KEY)).not.toBeNull();
    expect(getSnapshot(A).entries).toHaveLength(1);
    await act(async () => d.resolve(json(200, { needs_setup: false })));
    await settled("5");
    expect(getSnapshot(A).entries).toHaveLength(1);
  });
});

describe("F19: no AssistantChat mounted", () => {
  it("(a) a same-tab login() as B leaves no key and an empty store", async () => {
    populateAs(A);
    mount();
    await settled("5");
    me = () => json(200, userB());
    await act(async () => { await auth.login("b", "pw"); });
    await settled("9");
    wiped();
  });

  it("(b) a reload whose restore yields B removes A's key", async () => {
    populateAs(A);
    clearAssistantSession(); // the reload: memory gone ...
    window.sessionStorage.setItem(KEY, JSON.stringify({ owner: A, lastMessageAt: Date.now(), seq: 1, entries: [{ kind: "user", id: "e1", text: "secret of A" }] }));
    refresh = () => json(200, { access_token: tok("9", 11) });
    me = () => json(200, userB());
    mount();
    await settled("9");
    wiped();
  });

  it("(c) a same-owner entry older than 2h is removed when the restore adopts it", async () => {
    window.sessionStorage.setItem(KEY, JSON.stringify({ owner: A, lastMessageAt: Date.now() - 3 * 3600_000, seq: 1, entries: [{ kind: "user", id: "e1", text: "stale" }] }));
    mount();
    await settled("5");
    wiped();
  });

  it("(d) refreshMe with the same ids during a streaming turn does not drop the turn's later events", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => { release = r; });
    chats.push(() => new Response(
      new ReadableStream({
        async start(c) {
          c.enqueue(enc.encode('event: message\ndata: {"text":"before"}\n\n'));
          await gate;
          c.enqueue(enc.encode('event: message\ndata: {"text":"after refreshMe"}\n\nevent: done\ndata: {}\n\n'));
          c.close();
        },
      }),
      { status: 200 },
    ));
    mount(true);
    await settled("5");
    fireEvent.change(screen.getByLabelText("Message the assistant"), { target: { value: "hi" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText("before");
    me = () => json(200, userA()); // a new user object with the same ids
    await act(async () => { await auth.refreshMe(); });
    await act(async () => release());
    expect(await screen.findByText("after refreshMe")).toBeInTheDocument();
    expect(getSnapshot(A).entries.map((e) => e.kind)).toEqual(["user", "assistant", "assistant"]);
  });
});

describe("F20: a late turn of the previous owner cannot touch the next owner's store", () => {
  it("A's openTurn rejects after A switched to B and B sent one message: B's store and key are unchanged", async () => {
    const heldA = deferred();
    chats.push(() => heldA.p, () => sse([["message", { text: "hello B" }]]));
    mount(true);
    await settled("5");
    fireEvent.change(screen.getByLabelText("Message the assistant"), { target: { value: "from A" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(chats).toHaveLength(1)); // A's request is out

    me = () => json(200, userB());
    await act(async () => { await auth.login("b", "pw"); });
    await settled("9");
    fireEvent.change(await screen.findByLabelText("Message the assistant"), { target: { value: "from B" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText("hello B");
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument());

    const snap = JSON.stringify(getSnapshot(B).entries);
    const key = window.sessionStorage.getItem(KEY);
    expect(getSnapshot(B).entries.length).toBeGreaterThan(0);

    await act(async () => heldA.resolve(json(409, { detail: { code: "agent_busy" } })));
    await act(async () => { await new Promise((r) => setTimeout(r, 30)); });
    expect(JSON.stringify(getSnapshot(B).entries)).toBe(snap);
    expect(window.sessionStorage.getItem(KEY)).toBe(key);
    expect(screen.queryByText("Not sent")).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry message" })).toBeNull();
  });
});
