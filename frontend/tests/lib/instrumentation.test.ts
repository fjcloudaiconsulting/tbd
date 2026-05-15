import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("@/lib/logger", () => ({
  logger: { error: vi.fn(), warn: vi.fn(), info: vi.fn(), debug: vi.fn() },
}));

import { logger } from "@/lib/logger";
import { onRequestError } from "@/instrumentation";

beforeEach(() => {
  vi.clearAllMocks();
});

describe("onRequestError", () => {
  it("emits frontend.ssr.error with only allowlisted fields", async () => {
    const err = Object.assign(new Error("boom"), { digest: "DIGEST-XYZ" });
    err.name = "RuntimeError";

    await onRequestError(
      err,
      { path: "/forecast-plans", method: "GET" },
      {
        routePath: "/forecast-plans",
        routeType: "page",
        renderSource: "react-server-components",
      },
    );

    expect(logger.error).toHaveBeenCalledTimes(1);
    const [event, fields] = (logger.error as unknown as { mock: { calls: [string, Record<string, unknown>][] } }).mock.calls[0];
    expect(event).toBe("frontend.ssr.error");

    // Allowlist assertion — these keys are the entire field set.
    expect(new Set(Object.keys(fields))).toEqual(
      new Set([
        "digest",
        "error_name",
        "error_message",
        "route_path",
        "route_type",
        "render_source",
        "request_path",
        "method",
      ]),
    );

    expect(fields.digest).toBe("DIGEST-XYZ");
    expect(fields.error_name).toBe("RuntimeError");
    expect(fields.error_message).toBe("boom");
    expect(fields.request_path).toBe("/forecast-plans");
    expect(fields.route_path).toBe("/forecast-plans");
    expect(fields.route_type).toBe("page");
    expect(fields.render_source).toBe("react-server-components");
    expect(fields.method).toBe("GET");
  });

  it("never logs request headers, cookies, tokens, or sensitive query values", async () => {
    const SECRET_COOKIE = "SECRET-REFRESH-TOKEN-VALUE";
    const SECRET_BEARER = "SECRET-AUTH-BEARER-VALUE";
    const SECRET_QUERY = "SECRET-PASSWORD-RESET-TOKEN";

    const err = new Error("boom");
    err.name = "RuntimeError";

    await onRequestError(
      err,
      {
        path: `/reset-password?token=${SECRET_QUERY}`,
        method: "GET",
        headers: {
          Cookie: `refresh_token=${SECRET_COOKIE}`,
          Authorization: `Bearer ${SECRET_BEARER}`,
        },
      },
      {
        routePath: "/reset-password",
        routeType: "page",
        renderSource: "react-server-components",
      },
    );

    expect(logger.error).toHaveBeenCalledTimes(1);
    const payload = (logger.error as unknown as { mock: { calls: [string, Record<string, unknown>][] } }).mock.calls[0][1];
    const serialized = JSON.stringify(payload);

    expect(serialized).not.toContain(SECRET_COOKIE);
    expect(serialized).not.toContain(SECRET_BEARER);
    // Strict policy: full query string stripped from request_path,
    // so SECRET_QUERY must not appear in the payload at all.
    expect(serialized).not.toContain(SECRET_QUERY);
    // Sanity: pathname survived without the query.
    expect(payload.request_path).toBe("/reset-password");
  });

  it("handles missing/optional fields without crashing", async () => {
    const err = new Error("oops");

    await onRequestError(err, {}, {});

    expect(logger.error).toHaveBeenCalledTimes(1);
    const fields = (logger.error as unknown as { mock: { calls: [string, Record<string, unknown>][] } }).mock.calls[0][1];
    expect(fields.error_message).toBe("oops");
    expect(fields.error_name).toBe("Error");
    expect(fields.digest).toBeUndefined();
    expect(fields.request_path).toBeUndefined();
  });
});
