import { createHash } from "node:crypto";

import { apiFetch } from "@/lib/api";
import {
  OPENROUTER_NONCE_KEY,
  openRouterStateRef,
  startOpenRouterConnect,
  takeOpenRouterNonce,
} from "@/lib/openrouter-connect";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

const b64url = (s: string) => Buffer.from(s).toString("base64url");

describe("openrouter-connect", () => {
  let savedLocation: Location;
  const assign = vi.fn();

  beforeAll(() => {
    savedLocation = window.location;
  });

  beforeEach(() => {
    sessionStorage.clear();
    assign.mockReset();
    vi.mocked(apiFetch).mockReset();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...savedLocation, assign },
    });
  });

  afterAll(() => {
    Object.defineProperty(window, "location", { configurable: true, value: savedLocation });
  });

  it("sends only the sha256 of a stored high-entropy nonce, then leaves for the returned url", async () => {
    vi.mocked(apiFetch).mockResolvedValue({ url: "https://openrouter.ai/auth?x=1" } as never);

    await startOpenRouterConnect();

    const nonce = sessionStorage.getItem(OPENROUTER_NONCE_KEY)!;
    expect(nonce).toMatch(/^[A-Za-z0-9_-]{22,128}$/);
    const [url, init] = vi.mocked(apiFetch).mock.calls[0];
    expect(url).toBe("/api/v1/settings/ai-providers/oauth/openrouter/start");
    expect(JSON.parse(init!.body as string)).toEqual({
      nonce_hash: createHash("sha256").update(nonce).digest("hex"),
    });
    expect(assign).toHaveBeenCalledWith("https://openrouter.ai/auth?x=1");
  });

  it("makes a new nonce on every start", async () => {
    vi.mocked(apiFetch).mockResolvedValue({ url: "https://openrouter.ai/auth" } as never);
    await startOpenRouterConnect();
    const first = sessionStorage.getItem(OPENROUTER_NONCE_KEY);
    await startOpenRouterConnect();
    expect(sessionStorage.getItem(OPENROUTER_NONCE_KEY)).not.toBe(first);
  });

  it("refuses to leave for anywhere but OpenRouter", async () => {
    vi.mocked(apiFetch).mockResolvedValue({ url: "https://evil.example/auth" } as never);
    await expect(startOpenRouterConnect()).rejects.toThrow();
    expect(assign).not.toHaveBeenCalled();
  });

  it("does not leave the page when start fails", async () => {
    vi.mocked(apiFetch).mockRejectedValue(new Error("nope"));
    await expect(startOpenRouterConnect()).rejects.toThrow("nope");
    expect(assign).not.toHaveBeenCalled();
  });

  it("hands the nonce out once", () => {
    sessionStorage.setItem(OPENROUTER_NONCE_KEY, "n".repeat(43));
    expect(takeOpenRouterNonce()).toBe("n".repeat(43));
    expect(takeOpenRouterNonce()).toBeNull();
  });

  it("reads the label ref from the state's jti and tolerates garbage", () => {
    const state = `${b64url('{"alg":"HS256"}')}.${b64url('{"jti":"ab12cd34ef56","sub":"1"}')}.sig`;
    expect(openRouterStateRef(state)).toBe("ab12cd34");
    expect(openRouterStateRef("not-a-jwt")).toBeNull();
    expect(openRouterStateRef(`a.${b64url('{"jti":1}')}.c`)).toBeNull();
  });
});
