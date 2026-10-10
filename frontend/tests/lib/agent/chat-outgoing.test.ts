/**
 * F-581-HIST at the history builder. Wrong implementations killed: trimming
 * by `.length` instead of UTF-8 bytes (the server counts bytes and would 422),
 * no 40-message cap, and a history that starts on an assistant message.
 */
import { MAX_BYTES, MAX_MESSAGES, outgoing, type Entry } from "@/lib/agent/chat";

const u = (id: string, text: string): Entry => ({ kind: "user", id, text });
const a = (id: string, text: string): Entry => ({ kind: "assistant", id, text });
const bytes = (s: string) => new TextEncoder().encode(s).length;

it("trims by UTF-8 bytes, not characters", () => {
  // Each entry is 20,000 chars of a 3-byte character: 60,000 bytes.
  const big = "€".repeat(20_000);
  const out = outgoing([u("1", big), a("2", "ok"), u("3", big)]);
  expect(out.reduce((n, m) => n + bytes(m.content), 0)).toBeLessThanOrEqual(MAX_BYTES);
  expect(out).toEqual([{ role: "user", content: big }]);
});

it("keeps at most 40 messages and always starts on a user message", () => {
  const entries: Entry[] = [];
  for (let i = 0; i < 30; i++) entries.push(u(`u${i}`, `q${i}`), a(`a${i}`, `r${i}`));
  entries.push(u("last", "now"));
  const out = outgoing(entries);
  expect(out.length).toBeLessThanOrEqual(MAX_MESSAGES);
  expect(out[0].role).toBe("user");
  expect(out.at(-1)).toEqual({ role: "user", content: "now" });
});

it("drops failed turns and never sends a blank message", () => {
  const out = outgoing([u("1", "a"), { kind: "user", id: "2", text: "b", failed: true }, a("3", "  "), u("4", "c")]);
  expect(out).toEqual([{ role: "user", content: "a\n\nc" }]);
});
