/**
 * F-581-CODE. Wrong implementation killed: reading the error code only when
 * the detail also carries a message, which leaves agent_busy, plan limits and
 * the AI dispatch refusals with `code === undefined`.
 */
import { ApiResponseError, apiFetch, setAccessToken } from "@/lib/api";

afterEach(() => {
  vi.unstubAllGlobals();
  setAccessToken(null);
});

it("keeps a code that arrives without a message", async () => {
  setAccessToken("t");
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
    new Response(JSON.stringify({ detail: { code: "agent_busy" } }), { status: 409 }),
  ));
  const err = await apiFetch("/api/v1/agent/chat", { method: "POST", body: "{}" }).catch((e) => e);
  expect(err).toBeInstanceOf(ApiResponseError);
  expect([err.status, err.code]).toEqual([409, "agent_busy"]);
});

it("raw returns the response untouched on success", async () => {
  setAccessToken("t");
  const res = new Response("event: done\ndata: {}\n\n", { status: 200 });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(res));
  expect(await apiFetch<Response>("/api/v1/agent/chat", { method: "POST", body: "{}", raw: true })).toBe(res);
});
