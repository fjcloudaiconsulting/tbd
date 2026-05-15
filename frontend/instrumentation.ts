/**
 * Next.js instrumentation — runs once on server startup, and intercepts
 * SSR/RSC errors via the v15+ onRequestError hook.
 *
 * Logs the server start event in structured JSON format, and emits a
 * sanitized `frontend.ssr.error` event for every SSR/RSC error so the
 * `error.digest` references that surface in the error boundary become
 * searchable in App Platform run logs.
 */
import { logger } from "./lib/logger";

export async function register() {
  if (process.env.NEXT_RUNTIME === "nodejs") {
    const entry = {
      timestamp: new Date().toISOString(),
      level: "info",
      logger: "frontend",
      event: "starting",
      app: "PFV2 Frontend",
      env: process.env.NODE_ENV,
      runtime: "nodejs",
    };
    process.stdout.write(JSON.stringify(entry) + "\n");
  }
}

/**
 * Structural shape of the Next.js 15+ onRequestError arguments. We avoid
 * importing the framework types directly to keep this file portable across
 * Next minor upgrades; the runtime payload is what matters.
 */
interface SsrRequestInfo {
  path?: string;
  method?: string;
  // headers/body intentionally NOT destructured below — see allowlist note.
  headers?: Record<string, string>;
}

interface SsrErrorContext {
  routerKind?: string; // "Pages Router" | "App Router"
  routePath?: string;
  routeType?: string; // "route" | "page" | "action"
  renderSource?: string; // "react-server-components" | "server-action" | ...
  revalidateReason?: string;
}

/**
 * Next.js 15+ hook. Fires on every SSR/RSC error (anything that would
 * otherwise surface as a digest in the error boundary). We log a
 * sanitized structured event so digests become searchable and the
 * underlying error name/message is recoverable from App Platform run logs.
 *
 * Privacy invariant: the destructured field list in the `logger.error`
 * call below is the ENTIRE allowlist. Do not add headers, cookies,
 * tokens, auth values, request bodies, response bodies, or query
 * values (which may carry password-reset tokens, magic-link codes,
 * SSO state, etc.).
 *
 * Query-string policy: STRICT. The full query string is stripped from
 * `request_path` regardless of contents. Selective scrub-listing is a
 * maintenance trap; the cost of losing query context for SSR errors
 * is small relative to the risk of leaking a sensitive token.
 */
export async function onRequestError(
  err: Error & { digest?: string },
  request: SsrRequestInfo,
  context: SsrErrorContext,
): Promise<void> {
  const rawPath = request?.path;
  const requestPath =
    typeof rawPath === "string" ? rawPath.split("?")[0] : rawPath;

  logger.error("frontend.ssr.error", {
    digest: err?.digest,
    error_name: err?.name ?? "Unknown",
    error_message: err?.message ?? "",
    route_path: context?.routePath,
    route_type: context?.routeType,
    render_source: context?.renderSource,
    request_path: requestPath,
    method: request?.method,
  });
}
