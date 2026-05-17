/**
 * Per-request CSP nonce reader for Server Components.
 *
 * The proxy at ``frontend/proxy.ts`` generates a fresh base64 nonce
 * per request and forwards it via the ``x-nonce`` request header.
 * ``next/headers`` ``headers()`` exposes that to Server Components,
 * which can attach it to inline ``<script>`` tags so they pass the
 * strict CSP shipped by the proxy (no ``'unsafe-inline'`` on
 * script-src in production).
 *
 * Behavior splits by build target:
 *
 *   * App Platform build (``next build`` via ``next.config.ts``):
 *     ``headers()`` is available at render time; this function
 *     returns the nonce string. Using ``headers()`` opts the page
 *     into dynamic rendering, which is required by the Next.js
 *     nonce contract.
 *
 *   * Apex static export (``next.config.apex.ts``,
 *     ``NEXT_PUBLIC_BUILD_TARGET=apex``): there is no per-request
 *     header at static-generation time. Calling ``headers()`` would
 *     throw and break the export. This function returns an empty
 *     string in that path; CloudFront response-headers policy
 *     handles CSP for the apex host (no nonce required).
 *
 * The function is async because Next.js 15+ ``headers()`` returns a
 * Promise. Returning ``""`` for "no nonce available" keeps the call
 * sites simple: ``const nonce = await readNonce(); if (nonce) ...``.
 */
import { headers } from "next/headers";

const APEX_BUILD = process.env.NEXT_PUBLIC_BUILD_TARGET === "apex";

export async function readNonce(): Promise<string> {
  if (APEX_BUILD) {
    // Apex static export has no request context. Returning "" lets
    // the layout render without a nonce attribute, which is fine
    // because CloudFront sets the apex CSP independently.
    return "";
  }
  try {
    const hdrs = await headers();
    return hdrs.get("x-nonce") ?? "";
  } catch {
    // Defensive: if a future caller invokes this outside a request
    // scope (e.g. a static page in the App Platform build), fall
    // back to no-nonce rather than crashing the render. The CSP
    // header on the response will still come from ``proxy.ts`` with
    // its own nonce; the inline script just won't pass that nonce
    // and would be blocked. Surface in logs so we notice.
    if (process.env.NODE_ENV !== "production") {
      // eslint-disable-next-line no-console
      console.warn(
        "readNonce: headers() unavailable outside request scope; " +
          "inline scripts in this render will lack a nonce.",
      );
    }
    return "";
  }
}
