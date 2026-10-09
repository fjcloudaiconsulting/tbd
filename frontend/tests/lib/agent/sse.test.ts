/**
 * F-581-SSE. Wrong implementation killed: parsing each network chunk on its
 * own, which drops or corrupts an event split across two chunks.
 */
import { readSse } from "@/lib/agent/sse";

function reader(chunks: string[]): ReadableStreamDefaultReader<Uint8Array> {
  const enc = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(c) {
      for (const ch of chunks) c.enqueue(enc.encode(ch));
      c.close();
    },
  }).getReader();
}

async function all(chunks: string[]) {
  const out = [];
  for await (const e of readSse(reader(chunks))) out.push(e);
  return out;
}

it("joins an event split across chunks, skips keepalives, accepts CRLF", async () => {
  expect(
    await all([
      ": keepalive\n\n",
      "event: tool_call\nda",
      'ta: {"name":"budgets_list"}\n',
      "\nevent: message\r\ndata: {\"text\":\"a\\nb\"}\r\n\r\n",
      "event: done\ndata: {}\n\n",
    ]),
  ).toEqual([
    { event: "tool_call", data: '{"name":"budgets_list"}' },
    { event: "message", data: '{"text":"a\\nb"}' },
    { event: "done", data: "{}" },
  ]);
});

it("a CRLF split across two chunks is one line break, not an event boundary", async () => {
  expect(await all(["event: preview\r", "\ndata: {}\r\n\r\n"])).toEqual([{ event: "preview", data: "{}" }]);
});

it("drops an unterminated trailing block", async () => {
  expect(await all(["event: message\ndata: {}"])).toEqual([]);
});
