/**
 * G3 (TBD-613). The server's input-limit codes have user-facing copy, and the
 * composer cap matches the server's 7,000-character message limit.
 */
import { MAX_INPUT_CHARS, errorText } from "@/lib/agent/chat";

it("caps the composer at the server's message limit", () => {
  expect(MAX_INPUT_CHARS).toBe(7000);
});

it.each([
  ["message_too_long", "Your message is over 7,000 characters. Shorten it and try again."],
  ["input_budget_exceeded", "Your message is too long for the assistant. Shorten it and try again."],
  ["input_budget", "This question needed more lookups than one reply allows. Ask in smaller steps or start a new chat."],
  ["empty_reply", "The assistant returned no answer. Try again."],
])("%s has its own copy", (code, text) => {
  expect(errorText(code, 413)).toBe(text);
  expect(text).not.toContain("—");
});
