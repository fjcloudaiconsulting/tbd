import { NextRequest } from "next/server";

import { middleware } from "@/middleware";


describe("frontend middleware", () => {
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

    middleware(request);

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

    middleware(request);

    const entry = JSON.parse(logSpy.mock.calls[0][0] as string);
    expect(entry.path).toBe("/dashboard");
    expect(entry.remote_addr).toBe("198.51.100.4");
    expect(entry).not.toHaveProperty("query");
    expect(entry).not.toHaveProperty("user_agent");
    expect(entry).not.toHaveProperty("referer");
  });
});
