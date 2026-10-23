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
  ["provider_status_429", /rate-limiting requests or is out of credit/],
  ["provider_status_402", /out of credit\. An admin can top it up/],
  ["provider_status_401", /rejected the key\. An admin can reconnect/],
  ["provider_status_403", /refused this request/],
  ["provider_status_503", /having trouble/],
  ["platform_ai_unavailable", /AI service isn.t available right now/],
])("%s has specific copy", (code, text) => {
  expect(errorText(code)).toMatch(text);
});

it("an unlisted 4xx provider status keeps the generic copy", () => {
  expect(errorText("provider_status_404")).toBe("Something went wrong. Try again.");
});
