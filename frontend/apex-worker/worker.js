// Runs only for /vd9r/* (wrangler.jsonc run_worker_first). Port of the CloudFront /vd9r/* behaviour:
// the Google tag gateway, proxied uncached to the tag's fps.goog origin (method, headers and body pass through).
// Must match GA_MEASUREMENT_ID and GA_GATEWAY_PATH in lib/analytics.ts (tests/apex-worker.test.ts checks it).
// No named exports: workerd rejects any export that is not a handler.
const GA_GATEWAY_PATH = "/vd9r/";
const GA_GATEWAY_ORIGIN = "https://G-GRXDVTVBLV.fps.goog";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith(GA_GATEWAY_PATH)) return env.ASSETS.fetch(request);

    const upstream = new Request(GA_GATEWAY_ORIGIN + url.pathname + url.search, request);
    // Google geolocates gateway traffic from these headers; a Worker subrequest does not carry the visitor's IP.
    // Visitor-sent copies are dropped first: Google trusts these headers, the combined one above the split pair.
    for (const h of ["X-Forwarded-CountryRegion", "X-Forwarded-Country", "X-Forwarded-Region"]) upstream.headers.delete(h);
    const { country, regionCode } = request.cf ?? {};
    if (country) upstream.headers.set("X-Forwarded-Country", country);
    if (regionCode) upstream.headers.set("X-Forwarded-Region", regionCode);
    return fetch(upstream);
  },
};
