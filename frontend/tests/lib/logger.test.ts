/**
 * Runtime detection for `@/lib/logger`.
 *
 * Next.js 16 imports `instrumentation.ts` (which imports the logger)
 * into BOTH the Node and Edge runtimes. The Edge runtime does not
 * expose `process.stdout` / `process.stderr`; accessing them would
 * crash at request time and triggers a static-analysis warning at
 * build time. The logger must therefore:
 *
 *   - emit JSON to stdout/stderr in the Node.js server runtime,
 *   - emit JSON via `console.log` / `console.error` in the Edge
 *     runtime (preserving aggregator parity),
 *   - emit human-readable lines via `console` in the browser.
 *
 * Runtime detection is via `process.env.NEXT_RUNTIME`
 * (`"nodejs"` | `"edge"`) plus the `typeof window` check.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const realRuntime = process.env.NEXT_RUNTIME;
const realWindow = (globalThis as { window?: unknown }).window;

// ``vi.spyOn`` has multiple overloaded signatures and inferring its
// return type for ``process.stdout.write`` (itself overloaded) confuses
// TS. Use ``MockInstance`` directly — the test only ever reads
// ``mock.calls`` and ``toHaveBeenCalled`` matchers off these.
type Spy = ReturnType<typeof vi.spyOn<any, any>>; // eslint-disable-line @typescript-eslint/no-explicit-any

describe("logger runtime detection", () => {
  let stdoutSpy: Spy;
  let stderrSpy: Spy;
  let consoleLogSpy: Spy;
  let consoleWarnSpy: Spy;
  let consoleErrorSpy: Spy;

  beforeEach(() => {
    vi.resetModules();
    stdoutSpy = vi
      .spyOn(process.stdout, "write")
      .mockImplementation(() => true);
    stderrSpy = vi
      .spyOn(process.stderr, "write")
      .mockImplementation(() => true);
    consoleLogSpy = vi.spyOn(console, "log").mockImplementation(() => {});
    consoleWarnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    process.env.NEXT_RUNTIME = realRuntime;
    if (realWindow === undefined) {
      delete (globalThis as { window?: unknown }).window;
    } else {
      (globalThis as { window?: unknown }).window = realWindow;
    }
    vi.restoreAllMocks();
  });

  it("writes JSON to process.stdout / process.stderr on Node.js server runtime", async () => {
    delete (globalThis as { window?: unknown }).window;
    process.env.NEXT_RUNTIME = "nodejs";

    const { logger } = await import("@/lib/logger");
    logger.info("server.event", { request_id: "abc" });
    logger.error("server.error", { request_id: "abc" });

    expect(stdoutSpy).toHaveBeenCalledTimes(1);
    expect(stderrSpy).toHaveBeenCalledTimes(1);
    expect(consoleLogSpy).not.toHaveBeenCalled();
    expect(consoleErrorSpy).not.toHaveBeenCalled();

    // Payload must be a single JSON line terminated by a newline,
    // matching the backend structlog format.
    const stdoutLine = stdoutSpy.mock.calls[0][0] as string;
    const stderrLine = stderrSpy.mock.calls[0][0] as string;
    expect(stdoutLine.endsWith("\n")).toBe(true);
    expect(stderrLine.endsWith("\n")).toBe(true);
    const parsed = JSON.parse(stdoutLine.trim()) as Record<string, unknown>;
    expect(parsed.level).toBe("info");
    expect(parsed.event).toBe("server.event");
    expect(parsed.request_id).toBe("abc");
    expect(parsed.timestamp).toEqual(expect.any(String));
  });

  it("uses console on Edge runtime — does NOT touch process.stdout / process.stderr", async () => {
    delete (globalThis as { window?: unknown }).window;
    process.env.NEXT_RUNTIME = "edge";

    const { logger } = await import("@/lib/logger");
    logger.info("edge.event", { request_id: "abc" });
    logger.warn("edge.warn");
    logger.error("edge.error", { digest: "xyz" });

    // The load-bearing invariant: zero writes to process streams in
    // Edge. Triggering them would crash the Edge runtime at request
    // time and was the reason `next dev` warned at PR #284's
    // instrumentation hook import path.
    expect(stdoutSpy).not.toHaveBeenCalled();
    expect(stderrSpy).not.toHaveBeenCalled();

    expect(consoleLogSpy).toHaveBeenCalledTimes(1);
    expect(consoleWarnSpy).toHaveBeenCalledTimes(1);
    expect(consoleErrorSpy).toHaveBeenCalledTimes(1);

    // Edge logs still ship structured JSON so the aggregator parser
    // can pick them up the same way it does the Node runtime lines.
    const infoArg = consoleLogSpy.mock.calls[0][0] as string;
    expect(() => JSON.parse(infoArg)).not.toThrow();
    const parsed = JSON.parse(infoArg) as Record<string, unknown>;
    expect(parsed.level).toBe("info");
    expect(parsed.event).toBe("edge.event");

    const errorArg = consoleErrorSpy.mock.calls[0][0] as string;
    const parsedErr = JSON.parse(errorArg) as Record<string, unknown>;
    expect(parsedErr.level).toBe("error");
    expect(parsedErr.event).toBe("edge.error");
    expect(parsedErr.digest).toBe("xyz");
  });

  it("uses human-readable console in the browser", async () => {
    // jsdom default: window defined. Explicitly set runtime to
    // something non-Node to prove the browser branch wins over the
    // runtime check.
    process.env.NEXT_RUNTIME = "edge";
    (globalThis as { window?: unknown }).window = realWindow ?? {};

    const { logger } = await import("@/lib/logger");
    logger.info("client.event", { user_id: 1 });
    logger.error("client.error");

    expect(stdoutSpy).not.toHaveBeenCalled();
    expect(stderrSpy).not.toHaveBeenCalled();
    expect(consoleLogSpy).toHaveBeenCalledTimes(1);
    expect(consoleErrorSpy).toHaveBeenCalledTimes(1);

    // Browser format is the human-readable "[level] event" prefix +
    // structured payload (not the raw JSON-line shape).
    const [prefix, payload] = consoleLogSpy.mock.calls[0] as [string, unknown];
    expect(prefix).toBe("[info] client.event");
    expect(payload).toEqual({ user_id: 1 });
  });
});
