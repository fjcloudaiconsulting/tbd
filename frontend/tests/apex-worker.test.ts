import { afterEach, describe, expect, it, vi } from "vitest";
import worker from "../apex-worker/worker.js";
import { GA_GATEWAY_PATH, GA_MEASUREMENT_ID } from "@/lib/analytics";

// INFRA-60: apex-worker/worker.js ports the CloudFront /vd9r/* behaviour (Google tag
// gateway proxy). Static assets never reach it in production (run_worker_first), so
// the proxy and its drift against lib/analytics.ts are what this file fences.

afterEach(() => vi.unstubAllGlobals());

function run(url: string, cf?: Record<string, string>) {
  const upstream = vi.fn(async () => new Response("gtag"));
  vi.stubGlobal("fetch", upstream);
  const assets = { fetch: vi.fn(async () => new Response("asset")) };
  const request = Object.assign(new Request(url, { headers: { "X-Test": "kept" } }), { cf });
  return { response: worker.fetch(request, { ASSETS: assets }), upstream, assets };
}

describe("apex worker", () => {
  it("proxies the gateway path, query intact, to the measurement ID's fps.goog origin", async () => {
    const { response, upstream, assets } = run(
      `https://tbd-landing.example.workers.dev${GA_GATEWAY_PATH}g/collect?v=2&tid=x`,
      { country: "NL", regionCode: "NH" },
    );
    expect(await (await response).text()).toBe("gtag");
    expect(assets.fetch).not.toHaveBeenCalled();
    const sent = (upstream.mock.calls[0] as unknown as [Request])[0];
    expect(sent.url).toBe(`https://${GA_MEASUREMENT_ID.toLowerCase()}.fps.goog${GA_GATEWAY_PATH}g/collect?v=2&tid=x`);
    expect(sent.headers.get("X-Test")).toBe("kept");
    expect(sent.headers.get("X-Forwarded-Country")).toBe("NL");
    expect(sent.headers.get("X-Forwarded-Region")).toBe("NH");
  });

  it("serves any other path from assets, never the upstream", async () => {
    const { response, upstream, assets } = run("https://tbd-landing.example.workers.dev/privacy/");
    expect(await (await response).text()).toBe("asset");
    expect(assets.fetch).toHaveBeenCalledOnce();
    expect(upstream).not.toHaveBeenCalled();
  });
});
