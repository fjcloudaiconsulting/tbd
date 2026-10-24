// Google sign-in (FE-02). The nonce binds the callback's fragment to the tab
// that started the flow: GET /api/v1/auth/google returns it and keeps a copy
// in an httpOnly cookie, the callback echoes that copy into the success
// fragment, and the callback page accepts the fragment only when it matches
// the nonce this tab stored. A link that plants someone else's token carries
// no nonce this tab knows, so it is refused.
import { apiFetch } from "@/lib/api";

export const GOOGLE_SSO_NONCE_KEY = "tbd.google-sso-nonce";

/** Starts the flow and leaves the page for Google. Throws on any failure. */
export async function startGoogleSignIn(): Promise<void> {
  const data = await apiFetch<{ redirect_url: string; nonce?: string }>("/api/v1/auth/google");
  try {
    if (data.nonce) sessionStorage.setItem(GOOGLE_SSO_NONCE_KEY, data.nonce);
    else sessionStorage.removeItem(GOOGLE_SSO_NONCE_KEY);
  } catch {
    // No sessionStorage: the callback refuses the result and the user can
    // sign in with a password instead.
  }
  window.location.href = data.redirect_url;
}

/** Reads and deletes the nonce: a callback can use it once. */
export function takeGoogleSsoNonce(): string | null {
  try {
    const nonce = sessionStorage.getItem(GOOGLE_SSO_NONCE_KEY);
    sessionStorage.removeItem(GOOGLE_SSO_NONCE_KEY);
    return nonce;
  } catch {
    return null;
  }
}
