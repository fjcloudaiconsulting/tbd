/**
 * Structured JSON logger for Next.js — matches backend/nginx log format.
 *
 * Server-side: uses pino for structured JSON to stdout.
 * Client-side: uses console.* (logs go to browser devtools, not server).
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

// Server-side: JSON to stdout. Client-side: structured console.
const isServer = typeof window === "undefined";

function log(level: LogLevel, event: string, data?: Record<string, unknown>) {
  const entry = formatEntry(level, event, data);

  if (isServer) {
    // JSON to stdout — matches backend structlog format
    const line = JSON.stringify(entry);
    if (level === "error") {
      process.stderr.write(line + "\n");
    } else {
      process.stdout.write(line + "\n");
    }
  } else {
    // Client-side — browser devtools
    const method = level === "error" ? "error" : level === "warn" ? "warn" : "log";
    console[method](`[${level}] ${event}`, data ?? "");
  }
}

export const logger = {
  debug: (event: string, data?: Record<string, unknown>) => log("debug", event, data),
  info: (event: string, data?: Record<string, unknown>) => log("info", event, data),
  warn: (event: string, data?: Record<string, unknown>) => log("warn", event, data),
  error: (event: string, data?: Record<string, unknown>) => log("error", event, data),
};
