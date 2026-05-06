import { NextRequest } from "next/server";

import { proxy } from "@/proxy";


describe("frontend proxy", () => {
  const logSpy = vi.spyOn(console, "log").mockImplementation(() => {});

  beforeEach(() => {
    logSpy.mockClear();
  });

  afterAll(() => {
    logSpy.mockRestore();
  });

  it("redacts sensitive query parameters and logs the first forwarded IP", () => {
    const request = new NextRequest(
      "https://example.com/verify-email?token=abc123&foo=bar&Code=7890",
      {
        headers: {
          "x-forwarded-for": "203.0.113.7, 10.0.0.9",
          "user-agent": "Vitest",
          referer: "https://example.com/login",
        },
      },
    );

    proxy(request);

    expect(logSpy).toHaveBeenCalledTimes(1);
    const entry = JSON.parse(logSpy.mock.calls[0][0] as string);
    const query = new URLSearchParams(entry.query);
    expect(entry.path).toBe("/verify-email");
    expect(entry.remote_addr).toBe("203.0.113.7");
    expect(entry.user_agent).toBe("Vitest");
    expect(query.get("token")).toBe("[REDACTED]");
    expect(query.get("Code")).toBe("[REDACTED]");
    expect(query.get("foo")).toBe("bar");
  });

  it("falls back to x-real-ip and omits undefined fields", () => {
    const request = new NextRequest("https://example.com/dashboard", {
      headers: {
        "x-real-ip": "198.51.100.4",
      },
    });

    proxy(request);

    const entry = JSON.parse(logSpy.mock.calls[0][0] as string);
    expect(entry.path).toBe("/dashboard");
    expect(entry.remote_addr).toBe("198.51.100.4");
    expect(entry).not.toHaveProperty("query");
    expect(entry).not.toHaveProperty("user_agent");
    expect(entry).not.toHaveProperty("referer");
  });

  it("preserves a reasonable inbound X-Request-Id and echoes it on the response (L4.9)", () => {
    const request = new NextRequest("https://example.com/dashboard", {
      headers: {
        "x-request-id": "trace-abc-123",
      },
    });

    const response = proxy(request);

    const entry = JSON.parse(logSpy.mock.calls[0][0] as string);
    expect(entry.request_id).toBe("trace-abc-123");
    expect(response.headers.get("x-request-id")).toBe("trace-abc-123");
  });

  it("generates a request id when none is inbound (L4.9)", () => {
    const request = new NextRequest("https://example.com/dashboard");

    const response = proxy(request);
    const entry = JSON.parse(logSpy.mock.calls[0][0] as string);
    expect(entry.request_id).toMatch(/^[a-f0-9]{32}$/);
    expect(response.headers.get("x-request-id")).toBe(entry.request_id);
  });

  it("rejects an inbound id that fails the safe-character regex (L4.9)", () => {
    // Header values with newlines are rejected by the platform Headers
    // API before our code runs, so we can't actually pass `\n` through.
    // Test a value the platform accepts but our regex rejects (spaces).
    const request = new NextRequest("https://example.com/dashboard", {
      headers: {
        "x-request-id": "abc inject",
      },
    });

    const response = proxy(request);
    const entry = JSON.parse(logSpy.mock.calls[0][0] as string);
    expect(entry.request_id).not.toBe("abc inject");
    expect(entry.request_id).toMatch(/^[a-f0-9]{32}$/);
    expect(response.headers.get("x-request-id")).toBe(entry.request_id);
  });

  it("rejects an inbound id past the bounded length (L4.9)", () => {
    const huge = "a".repeat(200);
    const request = new NextRequest("https://example.com/dashboard", {
      headers: {
        "x-request-id": huge,
      },
    });

    const response = proxy(request);
    const entry = JSON.parse(logSpy.mock.calls[0][0] as string);
    expect(entry.request_id).not.toBe(huge);
    expect(entry.request_id.length).toBeLessThanOrEqual(64);
    expect(response.headers.get("x-request-id")).toBe(entry.request_id);
  });
});
