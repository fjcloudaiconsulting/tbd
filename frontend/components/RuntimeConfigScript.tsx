import { readEnvConfig } from "@/lib/runtime-config";

// Server component: emits the per-request runtime config for client code.
// "<" is escaped so no value can close the script element.
export function RuntimeConfigScript({ nonce }: { nonce?: string }) {
  const json = JSON.stringify(readEnvConfig()).replace(/</g, "\\u003c");
  return (
    <script
      {...(nonce ? { nonce } : {})}
      dangerouslySetInnerHTML={{ __html: `window.__TBD_CONFIG__=${json};` }}
    />
  );
}
