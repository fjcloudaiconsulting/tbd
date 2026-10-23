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
});

// TBD-662. The provider's HTTP status rides the SSE code as provider_status_<n>.
it.each([
  ["provider_status_429", /out of credit or rate-limited/],
  ["provider_status_401", /invalid or lacks access/],
  ["provider_status_403", /invalid or lacks access/],
  ["provider_status_503", /having trouble/],
])("%s has provider-specific copy", (code, text) => {
  expect(errorText(code)).toMatch(text);
});
