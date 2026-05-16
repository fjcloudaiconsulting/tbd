/**
 * Structured JSON logger for Next.js — matches backend/nginx log format.
 *
 * Runtime targets, all distinguished here because each one exposes a
 * different I/O surface:
 *
 *   - Node.js server runtime (`NEXT_RUNTIME === "nodejs"`): JSON line
 *     written via ``process.stdout.write`` / ``process.stderr.write``
 *     so the deployed App Platform log shipper sees the same shape as
 *     the backend structlog output.
 *   - Edge runtime (`NEXT_RUNTIME === "edge"`, e.g. middleware): the
 *     Edge runtime does NOT expose ``process.stdout`` / ``process.stderr``.
 *     Touching them would crash at request time and triggers a
 *     ``next dev`` static-analysis warning at build time. Emit the same
 *     JSON line via ``console.*`` so the log aggregator parses it
 *     identically.
 *   - Browser (``window`` defined): human-readable ``[level] event``
 *     prefix + structured payload via ``console.*`` for devtools
 *     ergonomics.
 *
 * Usage:
 *   import { logger } from "@/lib/logger";
 *   logger.info("page loaded", { path: "/dashboard" });
 *   logger.error("fetch failed", { url, status });
 */

type LogLevel = "debug" | "info" | "warn" | "error";

interface LogEntry {
  timestamp: string;
  level: LogLevel;
  logger: string;
  event: string;
  [key: string]: unknown;
}

function formatEntry(
  level: LogLevel,
  event: string,
  data?: Record<string, unknown>
): LogEntry {
  return {
    timestamp: new Date().toISOString(),
    level,
    logger: "frontend",
    event,
    ...data,
  };
}

function consoleMethodFor(level: LogLevel): "error" | "warn" | "log" {
  return level === "error" ? "error" : level === "warn" ? "warn" : "log";
}

// Access ``process`` via ``globalThis`` + dynamic property indexing so
// the Edge runtime's Turbopack analyzer cannot statically resolve the
// ``stdout`` / ``stderr`` member references. The analyzer treats any
// direct token like ``process.stderr`` as a Node-only API and emits a
// build-time warning, even when the reference sits inside an
// ``if (NEXT_RUNTIME === "nodejs")`` guard. Indirecting through
// ``globalThis`` removes the warning without giving up runtime
// correctness.
//
// The runtime guard still wins: the Edge branch (below) is taken when
// ``process.env.NEXT_RUNTIME !== "nodejs"`` so these properties are
// only read on the Node runtime that actually exposes them.
interface NodeWriteStream {
  write(chunk: string): boolean;
}
const _globalProcess = (globalThis as { process?: NodeJS.Process }).process;
const _nodeStdout: NodeWriteStream | undefined = _globalProcess?.[
  "stdout" as "stdout"
];
const _nodeStderr: NodeWriteStream | undefined = _globalProcess?.[
  "stderr" as "stderr"
];

function log(level: LogLevel, event: string, data?: Record<string, unknown>) {
  const entry = formatEntry(level, event, data);

  // Browser branch first — ``window`` presence is the cleanest signal.
  if (typeof window !== "undefined") {
    console[consoleMethodFor(level)](`[${level}] ${event}`, data ?? "");
    return;
  }

  // Server-side. Distinguish Node.js (has stdout/stderr) from Edge
  // (does not). ``process.env.NEXT_RUNTIME`` is the documented runtime
  // discriminant; the secondary ``_nodeStdout`` truthiness check is
  // defence-in-depth in case NEXT_RUNTIME is unset (e.g. when the
  // bundle is loaded outside Next, such as the vitest test runtime).
  const line = JSON.stringify(entry);
  if (
    _globalProcess?.env?.NEXT_RUNTIME === "nodejs" &&
    _nodeStdout &&
    _nodeStderr
  ) {
    if (level === "error") {
      _nodeStderr.write(line + "\n");
    } else {
      _nodeStdout.write(line + "\n");
    }
    return;
  }

  // Edge runtime (or any other non-Node server context — e.g. tests
  // that delete ``window`` without setting NEXT_RUNTIME). ``console.*``
  // is the only universally-available sink and the Edge log shipper
  // captures it as a structured JSON line.
  console[consoleMethodFor(level)](line);
}

export const logger = {
  debug: (event: string, data?: Record<string, unknown>) => log("debug", event, data),
  info: (event: string, data?: Record<string, unknown>) => log("info", event, data),
  warn: (event: string, data?: Record<string, unknown>) => log("warn", event, data),
  error: (event: string, data?: Record<string, unknown>) => log("error", event, data),
};
