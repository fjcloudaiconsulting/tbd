"use client";

// Inline usage help (spec §9 — v1 has no public product docs). Covers curl
// usage, the coarse read/write scope semantics, the caveat that some POST
// endpoints are semantic reads and still need a write token, and the note
// that optional-auth / public endpoints don't recognize PATs.

import { card, cardHeader, cardTitle } from "@/lib/styles";

const CURL_EXAMPLE = `curl https://thebetterdecision.com/api/v1/accounts \\
  -H "Authorization: Bearer pat_your_token_here"`;

export default function UsageHelp() {
  return (
    <div className={`${card} mb-6`} data-testid="usage-help">
      <div className={cardHeader}>
        <h2 className={cardTitle}>Using your token</h2>
      </div>
      <div className="space-y-4 p-6 text-sm text-text-secondary">
        <div>
          <p className="mb-2 text-text-primary">
            Send the token as a bearer credential on any authenticated API call:
          </p>
          <pre className="overflow-x-auto rounded-md border border-border bg-surface-raised px-3 py-2 text-xs text-text-primary">
            <code>{CURL_EXAMPLE}</code>
          </pre>
        </div>

        <div>
          <h3 className="font-semibold text-text-primary">Scopes</h3>
          <ul className="mt-1 list-disc space-y-1 pl-5">
            <li>
              <span className="font-medium text-text-primary">Read-only</span>{" "}
              allows safe <code>GET</code>/<code>HEAD</code> requests.
            </li>
            <li>
              <span className="font-medium text-text-primary">Read &amp; write</span>{" "}
              additionally allows <code>POST</code>/<code>PUT</code>/
              <code>PATCH</code>/<code>DELETE</code>.
            </li>
          </ul>
          <p className="mt-2">
            Scope is a coarse abuse-limiter, not a data boundary: even a
            read-only token can read every organization&apos;s data.
          </p>
        </div>

        <div>
          <h3 className="font-semibold text-text-primary">Caveats</h3>
          <ul className="mt-1 list-disc space-y-1 pl-5">
            <li>
              Some <code>POST</code> endpoints are semantically reads (report
              queries, exports). In v1 those POST endpoints still require a{" "}
              <span className="font-medium text-text-primary">Read &amp; write</span>{" "}
              token.
            </li>
            <li>
              Optional-auth and public endpoints (for example{" "}
              <code>/auth/status</code>) do not recognize PATs — they resolve as
              anonymous. Point automation at the main authenticated API.
            </li>
            <li>
              Tokens are independent of your password. Rotate before expiry by
              minting a replacement, switching automation over, then revoking
              the old one.
            </li>
          </ul>
        </div>
      </div>
    </div>
  );
}
