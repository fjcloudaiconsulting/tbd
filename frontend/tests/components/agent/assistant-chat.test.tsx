/**
 * TBD-581 assistant chat. Fences F-U1, F-U3 (model text is plain text, no
 * links), guards F-U2 (preview card from server fields, currency first), F-U4
 * (fetch + reader with the Bearer header, no EventSource, aria-live, focus on
 * the preview, axe clean), plus F-581-DBL, F-581-STALE, F-581-UNTRUSTED and
 * F-581-HIST at the transport.
 *
 * Drives the REAL apiFetch over a mocked global fetch, so the Bearer header
 * and the body reader are the production path.
 */
import { StrictMode } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import axe from "axe-core";

import AssistantChat from "@/components/agent/AssistantChat";
import { setAccessToken } from "@/lib/api";
import { setBalancesHidden } from "@/lib/format";
import type { StagedAction } from "@/lib/agent/types";

const enc = new TextEncoder();

function sse(events: Array<[string, unknown]>, { done = true } = {}): Response {
  const body = events.map(([e, d]) => `event: ${e}\ndata: ${JSON.stringify(d)}\n\n`).join("")
    + (done ? "event: done\ndata: {}\n\n" : "");
  return new Response(
    new ReadableStream({
      start(c) {
        c.enqueue(enc.encode(": keepalive\n\n"));
        c.enqueue(enc.encode(body));
        c.close();
      },
    }),
    { status: 200, headers: { "content-type": "text/event-stream" } },
  );
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
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

const TX_ACTION: StagedAction = {
  action_id: "t1",
  summary: "Change the category of transaction 7 from category 3 to category 4",
  changes: [
    { entity: "transactions", id: 7, field: "category_id", before: 3, after: 4 },
    { entity: "category_rules", id: { untrusted: "<img src=x onerror=alert(1)>" }, field: "category_id", before: null, after: 4 },
  ],
  warnings: ["Also updates the organization's categorization rule for this description, which categorizes future imports."],
  context: {
    description: { untrusted: "<img src=x onerror=alert(1)>" },
    from: { category_name: { untrusted: "Food" } },
    to: { category_name: { untrusted: "Dining" } },
  },
  expires_at: "2026-10-09T12:10:00",
  requires_confirmation: true,
};

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  setAccessToken("test-token");
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  setAccessToken(null);
});

async function ask(text: string) {
  fireEvent.change(screen.getByLabelText("Message the assistant"), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument());
}

const chatBodies = () =>
  fetchMock.mock.calls
    .filter(([url]) => String(url).endsWith("/api/v1/agent/chat"))
    .map(([, init]) => JSON.parse((init as RequestInit).body as string));

describe("model text", () => {
  it("F-U1: markdown images, raw HTML and bare URLs render as literal text", async () => {
    const model = "![x](https://e.test/?q=1) <img src=\"https://e.test/p.png\"> see https://e.test/leak";
    fetchMock.mockResolvedValueOnce(sse([["message", { text: model }]]));
    render(<AssistantChat />);
    await ask("hi");

    const log = screen.getByRole("log");
    expect(within(log).getByTestId("assistant-text").textContent).toBe(model);
    expect(log.querySelector("img")).toBeNull();
    expect(log.querySelector("a")).toBeNull();
    // Nothing fetched any URL the model wrote.
    for (const [url] of fetchMock.mock.calls) expect(String(url)).not.toContain("e.test");
  });

  it("F-U3: URLs and app paths in model text are not links; the preview card's drill-down is", async () => {
    fetchMock.mockResolvedValueOnce(sse([
      ["message", { text: "Open https://evil.test or /transactions?transaction_id=1" }],
      ["tool_call", { name: "transactions_set_category" }],
      ["preview", { action: TX_ACTION }],
    ]));
    render(<AssistantChat />);
    await ask("recategorize");

    const text = screen.getByTestId("assistant-text");
    expect(text.querySelector("a")).toBeNull();
    const links = within(screen.getByRole("log")).getAllByRole("link");
    expect(links.map((a) => a.getAttribute("href"))).toEqual(["/transactions?transaction_id=7"]);
    expect(within(screen.getByTestId("preview-card")).getByRole("link")).toBe(links[0]);
  });
});

describe("preview card", () => {
  it("F-U2: renders server fields with the currency leading, not the model's words", async () => {
    fetchMock.mockResolvedValueOnce(sse([
      ["message", { text: "I'll set it to 9999 dollars." }],
      ["tool_call", { name: "budgets_update_amount" }],
      ["preview", { action: BUDGET_ACTION }],
    ]));
    render(<AssistantChat />);
    await ask("raise groceries");

    const card = screen.getByTestId("preview-card");
    expect(card.textContent).not.toContain("9999");
    expect(card.textContent).toContain("Groceries");
    expect(card.textContent).toMatch(/€400\.00/);
    expect(card.textContent).toMatch(/€1,234\.00/);
    // The server summary trails the currency; the card never shows it.
    expect(card.textContent).not.toContain("1234.00 EUR");
  });

  it("F-581-UNTRUSTED: wrapped strings, including a change id, render as text", async () => {
    fetchMock.mockResolvedValueOnce(sse([
      ["tool_call", { name: "transactions_set_category" }],
      ["preview", { action: TX_ACTION }],
    ]));
    render(<AssistantChat />);
    await ask("go");

    const card = screen.getByTestId("preview-card");
    expect(card.querySelector("img")).toBeNull();
    expect(card.textContent).toContain("<img src=x onerror=alert(1)>");
    // Names by id from context; the rule had no category yet.
    expect(card.textContent).toContain("Food");
    expect(card.textContent).toContain("Dining");
    expect(card.textContent).toContain("No rule");
    expect(card.textContent).toContain("categorization rule");
  });

  it("F-581-UNTRUSTED: a category the server did not name shows by id, never by position", async () => {
    const ruleElsewhere: StagedAction = {
      ...TX_ACTION,
      changes: [TX_ACTION.changes[0], { ...TX_ACTION.changes[1], before: 9 }],
    };
    fetchMock.mockResolvedValueOnce(sse([
      ["tool_call", { name: "transactions_set_category" }],
      ["preview", { action: ruleElsewhere }],
    ]));
    render(<AssistantChat />);
    await ask("go");
    const rows = screen.getByTestId("preview-card").querySelectorAll("dd");
    expect(rows[0].textContent).toContain("Food");
    expect(rows[1].textContent).toContain("category #9");
    expect(rows[1].textContent).not.toContain("Food");
  });

  it("F-581-DBL: a double click applies once", async () => {
    fetchMock.mockResolvedValueOnce(sse([["tool_call", { name: "budgets_update_amount" }], ["preview", { action: BUDGET_ACTION }]]));
    render(<AssistantChat />);
    await ask("go");
    let release!: (r: Response) => void;
    fetchMock.mockImplementationOnce(() => new Promise<Response>((r) => { release = r; }));

    const apply = screen.getByRole("button", { name: "Apply change" });
    fireEvent.click(apply);
    fireEvent.click(apply);
    await waitFor(() => expect(release).toBeTypeOf("function"));
    fireEvent.click(apply);
    await act(async () => release(json(200, { action_id: "a1", status: "done" })));

    const confirms = fetchMock.mock.calls.filter(([u]) => String(u).includes("/confirm"));
    expect(confirms).toHaveLength(1);
    await waitFor(() => expect(screen.getByText("Applied")).toBeInTheDocument());
  });

  it("F-581-BLIND: with balances hidden a money change cannot be applied unseen; showing them re-enables it", async () => {
    fetchMock.mockResolvedValueOnce(sse([["tool_call", { name: "budgets_update_amount" }], ["preview", { action: BUDGET_ACTION }]]));
    render(<AssistantChat />);
    await ask("go");
    act(() => setBalancesHidden(true));
    try {
      const card = screen.getByTestId("preview-card");
      expect(card.textContent).not.toMatch(/1,234/);
      const apply = screen.getByRole("button", { name: "Apply change" });
      expect(apply).toHaveAttribute("aria-disabled", "true");
      expect(card.textContent).toMatch(/Amounts are hidden/);
      fireEvent.click(apply);
      await act(async () => {});
      expect(fetchMock.mock.calls.filter(([u]) => String(u).includes("/confirm"))).toHaveLength(0);
      // Discard stays live.
      expect(screen.getByRole("button", { name: "Discard" })).not.toHaveAttribute("aria-disabled", "true");
    } finally {
      act(() => setBalancesHidden(false));
    }
    fetchMock.mockResolvedValueOnce(json(200, { action_id: "a1", status: "done" }));
    fireEvent.click(screen.getByRole("button", { name: "Apply change" }));
    await waitFor(() => expect(screen.getByText("Applied")).toBeInTheDocument());
  });

  it.each([
    [500, "internal", "Not applied", true],
    [410, "action_expired", "Expired", true],
    [409, "action_in_progress", "Already decided", true],
    [429, "confirm_rate_limited", null, false],
    [503, "limits_unavailable", null, false],
  ])("a %s %s confirm is %s", async (status, code, badge, final) => {
    fetchMock.mockResolvedValueOnce(sse([["tool_call", { name: "budgets_update_amount" }], ["preview", { action: BUDGET_ACTION }]]));
    render(<AssistantChat />);
    await ask("go");
    fetchMock.mockResolvedValueOnce(json(status as number, { detail: { code, message: "" } }));
    fireEvent.click(screen.getByRole("button", { name: "Apply change" }));
    if (final) {
      await waitFor(() => expect(screen.getByText(badge as string)).toBeInTheDocument());
      expect(screen.queryByRole("button", { name: "Apply change" })).toBeNull();
    } else {
      await waitFor(() => expect(screen.getByRole("alert").textContent).toBe("The change was not applied."));
      expect(screen.getByRole("button", { name: "Apply change" })).not.toHaveAttribute("aria-disabled", "true");
    }
  });

  it("F-581-STALE: a stale preview swaps in the fresh action and the next apply posts its id", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      fetchMock.mockResolvedValueOnce(sse([["tool_call", { name: "budgets_update_amount" }], ["preview", { action: BUDGET_ACTION }]]));
      render(<AssistantChat />);
      await ask("go");
      const fresh = { ...BUDGET_ACTION, action_id: "a2", changes: [{ ...BUDGET_ACTION.changes[0], before: "500.00" }] };
      fetchMock.mockResolvedValueOnce(json(409, { detail: { code: "preview_stale", message: "the data changed", ...fresh } }));
      fireEvent.click(screen.getByRole("button", { name: "Apply change" }));
      await waitFor(() => expect(screen.getByRole("status").textContent).toMatch(/data changed/));
      expect(screen.getByTestId("preview-card").textContent).toMatch(/€500\.00/);
      expect(document.activeElement).toBe(screen.getByTestId("preview-card"));

      // An immediate second click (the tail of a double click) is ignored.
      fireEvent.click(screen.getByRole("button", { name: "Apply change" }));
      expect(fetchMock.mock.calls.filter(([u]) => String(u).includes("/a2/confirm"))).toHaveLength(0);

      await act(async () => { vi.advanceTimersByTime(800); });
      fetchMock.mockResolvedValueOnce(json(200, { action_id: "a2", status: "done" }));
      fireEvent.click(screen.getByRole("button", { name: "Apply change" }));
      await waitFor(() => expect(screen.getByText("Applied")).toBeInTheDocument());
      expect(fetchMock.mock.calls.map(([u]) => String(u)).filter((u) => u.includes("/confirm")))
        .toEqual(["/api/v1/agent/actions/a1/confirm", "/api/v1/agent/actions/a2/confirm"]);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("transport and accessibility", () => {
  it("F-U4: fetch with Bearer and a body reader, never EventSource; aria-live; focus to the preview; axe clean", async () => {
    const es = vi.fn();
    vi.stubGlobal("EventSource", es);
    const getReader = vi.spyOn(ReadableStream.prototype, "getReader");
    fetchMock.mockResolvedValueOnce(sse([
      ["message", { text: "Here is the change." }],
      ["tool_call", { name: "budgets_update_amount" }],
      ["preview", { action: BUDGET_ACTION }],
    ]));
    const { container } = render(<AssistantChat />);
    await ask("raise it");

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toMatch(/\/api\/v1\/agent\/chat$/);
    expect(new Headers((init as RequestInit).headers).get("Authorization")).toBe("Bearer test-token");
    expect(getReader).toHaveBeenCalled();
    expect(es).not.toHaveBeenCalled();
    expect(screen.getByRole("log").getAttribute("aria-live")).toBe("polite");
    expect(document.activeElement).toBe(screen.getByTestId("preview-card"));

    let result!: axe.AxeResults;
    await act(async () => {
      result = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
    });
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.html).join(" | ")}`)).toEqual([]);
    getReader.mockRestore();
  });

  it("F-581-HIST: a preview-only turn is remembered as a server-written line, never blank", async () => {
    fetchMock.mockResolvedValueOnce(sse([["tool_call", { name: "budgets_update_amount" }], ["preview", { action: BUDGET_ACTION }]]));
    fetchMock.mockResolvedValueOnce(sse([["tool_call", { name: "budgets_list" }], ["tool_result", { name: "budgets_list", ok: true, rows: 2 }]]));
    fetchMock.mockResolvedValueOnce(sse([["message", { text: "ok" }]]));
    render(<AssistantChat />);
    await ask("first");
    await ask("second"); // ends ok with no message at all
    await ask("third");

    const [, second, third] = chatBodies();
    expect(second.messages).toEqual([
      { role: "user", content: "first" },
      { role: "assistant", content: `Proposed: ${BUDGET_ACTION.summary}. The user has not decided yet.` },
      { role: "user", content: "second" },
    ]);
    // The silent turn adds no assistant entry; the two user turns merge.
    expect(third.messages.at(-1)).toEqual({ role: "user", content: "second\n\nthird" });
    for (const b of chatBodies()) for (const m of b.messages) expect(m.content.trim()).not.toBe("");
  });

  it("a refusal before the stream shows its code's message and offers a retry", async () => {
    fetchMock.mockResolvedValueOnce(json(409, { detail: { code: "agent_busy" } }));
    render(<AssistantChat />);
    await ask("hello");
    expect(screen.getByText(/still running for your organization/)).toBeInTheDocument();
    expect(screen.getByText("Not sent")).toBeInTheDocument();
    fetchMock.mockResolvedValueOnce(sse([["message", { text: "hi" }]]));
    fireEvent.click(screen.getByRole("button", { name: "Retry message" }));
    await waitFor(() => expect(screen.getByTestId("assistant-text").textContent).toBe("hi"));
    expect(chatBodies().at(-1).messages).toEqual([{ role: "user", content: "hello" }]);
  });

  it("F-U4: an event renders while the stream is still open (read incrementally, not buffered)", async () => {
    let finish!: () => void;
    const gate = new Promise<void>((r) => { finish = r; });
    fetchMock.mockResolvedValueOnce(new Response(new ReadableStream({
      async start(c) {
        c.enqueue(enc.encode('event: message\ndata: {"text":"first"}\n\n'));
        await gate;
        c.enqueue(enc.encode("event: done\ndata: {}\n\n"));
        c.close();
      },
    }), { status: 200 }));
    render(<AssistantChat />);
    fireEvent.change(screen.getByLabelText("Message the assistant"), { target: { value: "hi" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(screen.getByTestId("assistant-text").textContent).toBe("first"));
    expect(screen.getByRole("button", { name: "Stop response" })).toBeInTheDocument();
    await act(async () => finish());
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument());
  });

  it("Stop before the stream opens still cancels it, so the turn frees the org lock", async () => {
    let respond!: (r: Response) => void;
    fetchMock.mockImplementationOnce(() => new Promise<Response>((r) => { respond = r; }));
    const cancel = vi.fn();
    render(<AssistantChat />);
    fireEvent.change(screen.getByLabelText("Message the assistant"), { target: { value: "hi" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(respond).toBeTypeOf("function"));
    fireEvent.click(screen.getByRole("button", { name: "Stop response" }));
    await act(async () => respond(new Response(new ReadableStream({ start() {}, cancel }), { status: 200 })));
    await waitFor(() => expect(cancel).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument());
  });

  it("under StrictMode (effects mount twice) a turn still streams to the end", async () => {
    fetchMock.mockResolvedValueOnce(sse([["message", { text: "strict ok" }]]));
    render(<StrictMode><AssistantChat /></StrictMode>);
    await ask("hi");
    expect(screen.getByTestId("assistant-text").textContent).toBe("strict ok");
    expect(screen.queryByText("Not sent")).toBeNull();
  });

  it("a stream that ends without done reports the dropped connection", async () => {
    fetchMock.mockResolvedValueOnce(sse([["tool_call", { name: "budgets_list" }]], { done: false }));
    render(<AssistantChat />);
    await ask("hello");
    expect(screen.getByText(/connection dropped/)).toBeInTheDocument();
  });
});
