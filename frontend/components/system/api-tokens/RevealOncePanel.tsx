"use client";

// Reveal-once panel (spec §9, SEC-R5). The plaintext token is shown exactly
// once, here, and never again — the backend keeps only an HMAC of it. Copy
// button + a blunt "you won't see this again" warning so the operator saves
// it before dismissing.

import { useState } from "react";

import type { MintTokenResponse } from "@/lib/types";
import { btnPrimary, btnSecondary } from "@/lib/styles";

import { scopeLabel } from "./expiry";

interface Props {
  result: MintTokenResponse;
  onDone: () => void;
}

export default function RevealOncePanel({ result, onDone }: Props) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(result.token);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard can be unavailable (permissions / insecure context). The
      // token is still selectable in the code block, so this is non-fatal.
    }
  }

  return (
    <div
      className="mb-6 rounded-lg border border-accent bg-accent/5 p-6"
      data-testid="reveal-panel"
    >
      <h2 className="text-sm font-semibold text-text-primary">
        Token created — copy it now
      </h2>
      <p className="mt-1 text-sm text-warning">
        This is the only time this token is shown. You won&apos;t see this token
        again — store it somewhere safe before you close this.
      </p>

      <div className="mt-4 flex flex-col gap-2 sm:flex-row sm:items-center">
        <code
          className="block flex-1 overflow-x-auto rounded-md border border-border bg-surface px-3 py-2 text-xs text-text-primary"
          data-testid="reveal-token"
        >
          {result.token}
        </code>
        <button
          type="button"
          onClick={copy}
          className={`${btnSecondary} shrink-0`}
          data-testid="reveal-copy"
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>

      <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-1 text-xs text-text-muted sm:grid-cols-4">
        <div>
          <dt className="font-semibold uppercase tracking-wider">Name</dt>
          <dd className="text-text-secondary">{result.name}</dd>
        </div>
        <div>
          <dt className="font-semibold uppercase tracking-wider">Scope</dt>
          <dd className="text-text-secondary">{scopeLabel(result.scope)}</dd>
        </div>
        <div>
          <dt className="font-semibold uppercase tracking-wider">Prefix</dt>
          <dd className="text-text-secondary">{result.prefix}</dd>
        </div>
        <div>
          <dt className="font-semibold uppercase tracking-wider">Expires</dt>
          <dd className="text-text-secondary">{result.expires_at.slice(0, 10)}</dd>
        </div>
      </dl>

      <div className="mt-5 flex justify-end">
        <button
          type="button"
          onClick={onDone}
          className={`${btnPrimary} sm:min-h-0`}
          data-testid="reveal-done"
        >
          Done
        </button>
      </div>
    </div>
  );
}
