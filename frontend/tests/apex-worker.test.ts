// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import path from "node:path";
import worker from "../apex-worker/worker.js";
import { GA_GATEWAY_PATH, GA_MEASUREMENT_ID } from "@/lib/analytics";

// INFRA-60: apex-worker/worker.js ports the CloudFront /vd9r/* behaviour (Google tag
// gateway proxy). Static assets never reach it in production (run_worker_first), so
// the proxy and its drift against lib/analytics.ts are what this file fences.

afterEach(() => vi.unstubAllGlobals());

const BASE = "https://tbd-landing.example.workers.dev";

function run(url: string, init: RequestInit = {}, cf?: Record<string, string>) {
  const upstream = vi.fn(async (_req: Request) => new Response("gtag"));
  vi.stubGlobal("fetch", upstream);
  const assets = { fetch: vi.fn(async () => new Response("asset")) };
  const request = Object.assign(new Request(url, init), { cf });
  return { response: worker.fetch(request, { ASSETS: assets }), upstream, assets };
}

describe("apex worker", () => {
  it("proxies the gtag loader URL to the measurement ID's fps.goog origin", async () => {
    const { response, upstream, assets } = run(`${BASE}${GA_GATEWAY_PATH}?id=${GA_MEASUREMENT_ID}`);
    expect(await (await response).text()).toBe("gtag");
    expect(assets.fetch).not.toHaveBeenCalled();
    expect(upstream.mock.calls[0][0].url).toBe(
      `https://${GA_MEASUREMENT_ID.toLowerCase()}.fps.goog${GA_GATEWAY_PATH}?id=${GA_MEASUREMENT_ID}`,
    );
  });

  it("passes a collect POST through with method, body, headers and Cloudflare geo", async () => {
    const { response, upstream } = run(
      `${BASE}${GA_GATEWAY_PATH}g/collect?v=2`,
      {
        method: "POST",
        body: "en=page_view",
        headers: { "X-Test": "kept", "X-Forwarded-CountryRegion": "US-CA", "X-Forwarded-Region": "CA" },
      },
      { country: "NL" },
    );
    await response;
    const sent = upstream.mock.calls[0][0];
    expect(sent.method).toBe("POST");
    expect(await sent.text()).toBe("en=page_view");
    expect(sent.headers.get("X-Test")).toBe("kept");
    expect(sent.headers.get("X-Forwarded-Country")).toBe("NL");
    // Spoofed geo from the visitor never reaches Google.
    expect(sent.headers.get("X-Forwarded-CountryRegion")).toBeNull();
    expect(sent.headers.get("X-Forwarded-Region")).toBeNull();
  });

  it("serves any other path from assets, never the upstream", async () => {
    const { response, upstream, assets } = run(`${BASE}/privacy/`);
    expect(await (await response).text()).toBe("asset");
    expect(assets.fetch).toHaveBeenCalledOnce();
    expect(upstream).not.toHaveBeenCalled();
  });

  it("routes exactly the gateway path to the worker in wrangler.jsonc", () => {
    // Without this, a GA_GATEWAY_PATH change keeps the tests above green while
    // production serves the new path from assets as a 404 (the #465 failure).
    const jsonc = readFileSync(path.join(__dirname, "../apex-worker/wrangler.jsonc"), "utf8");
    const config = JSON.parse(jsonc.replace(/^\s*\/\/.*$/gm, ""));
    expect(config.assets.run_worker_first).toEqual([`${GA_GATEWAY_PATH}*`]);
  });
});
