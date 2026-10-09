// Minimal server-sent-events reader for the assistant turn (TBD-581). The
// chat POST carries a Bearer header, which EventSource cannot send, so the
// stream is read from fetch's body. Events are `event:`/`data:` blocks split by
// a blank line; `:` lines are keepalive comments.

export interface SseEvent {
  event: string;
  data: string;
}

export function parseSseBlock(block: string): SseEvent | null {
  let event = "message";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (line === "" || line.startsWith(":")) continue;
    const i = line.indexOf(":");
    const field = i === -1 ? line : line.slice(0, i);
    const value = i === -1 ? "" : line.slice(i + 1).replace(/^ /, "");
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }
  return data.length ? { event, data: data.join("\n") } : null;
}

// Yields complete events as they arrive; a block split across chunks waits
// for its terminating blank line.
export async function* readSse(
  reader: ReadableStreamDefaultReader<Uint8Array>,
): AsyncGenerator<SseEvent> {
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n?/g, "\n");
    let end: number;
    while ((end = buffer.indexOf("\n\n")) !== -1) {
      const parsed = parseSseBlock(buffer.slice(0, end));
      buffer = buffer.slice(end + 2);
      if (parsed) yield parsed;
    }
  }
}
