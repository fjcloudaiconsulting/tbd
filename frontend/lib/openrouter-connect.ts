// OpenRouter one-click connect (TBD-632). The nonce binds the callback to the
// tab that started the flow: the server keeps only its sha256, the callback
// sends the nonce itself. Neither ever appears in a URL.
import { apiFetch } from "@/lib/api";

const BASE = "/api/v1/settings/ai-providers/oauth/openrouter";
export const OPENROUTER_NONCE_KEY = "tbd.openrouter-oauth-nonce";
export const OPENROUTER_KEYS_URL = "https://openrouter.ai/settings/keys";
export const OPENROUTER_PRIVACY_URL = "https://openrouter.ai/settings/privacy";

function base64url(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

/** Starts the flow and leaves the page for OpenRouter. Throws on any failure. */
export async function startOpenRouterConnect(): Promise<void> {
  const nonce = base64url(crypto.getRandomValues(new Uint8Array(32)));
  sessionStorage.setItem(OPENROUTER_NONCE_KEY, nonce);
  const { url } = await apiFetch<{ url: string }>(`${BASE}/start`, {
    method: "POST",
    body: JSON.stringify({ nonce_hash: await sha256Hex(nonce) }),
  });
  window.location.assign(url);
}

/** Reads and deletes the nonce: a callback can use it once. */
export function takeOpenRouterNonce(): string | null {
  try {
    const nonce = sessionStorage.getItem(OPENROUTER_NONCE_KEY);
    sessionStorage.removeItem(OPENROUTER_NONCE_KEY);
    return nonce;
  } catch {
    return null;
  }
}

/** The short ref in the key's OpenRouter label, read from the state for display only. */
export function openRouterStateRef(state: string): string | null {
  try {
    const payload = state.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    const jti = (JSON.parse(atob(payload)) as { jti?: unknown }).jti;
    return typeof jti === "string" && jti.length >= 8 ? jti.slice(0, 8) : null;
  } catch {
    return null;
  }
}

export function completeOpenRouterConnect<T>(code: string, state: string, nonce: string) {
  return apiFetch<T>(`${BASE}/complete`, {
    method: "POST",
    body: JSON.stringify({ code, state, nonce }),
  });
}
