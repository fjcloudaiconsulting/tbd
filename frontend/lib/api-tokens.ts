// Thin apiFetch wrappers for the superadmin PAT management UI (spec
// `specs/2026-07-21-superadmin-api-tokens-design.md`). Mirrors the routes in
// `backend/app/routers/api_tokens.py` — all four are superadmin-gated AND
// interactive-session-only (a PAT can never mint/list/revoke tokens).
import { apiFetch } from "@/lib/api";
import type {
  ApiToken,
  ListEnvelope,
  MintTokenRequest,
  MintTokenResponse,
} from "@/lib/types";

export const API_TOKENS_BASE = "/api/v1/system/api-tokens";

export async function listApiTokens(): Promise<ListEnvelope<ApiToken>> {
  return apiFetch<ListEnvelope<ApiToken>>(API_TOKENS_BASE);
}

// Mint a reveal-once PAT. The plaintext token comes back exactly once in
// `MintTokenResponse.token`; callers must surface it immediately and never
// persist it. A wrong/missing step-up proof rejects with a generic 401.
export async function mintApiToken(
  req: MintTokenRequest,
): Promise<MintTokenResponse> {
  return apiFetch<MintTokenResponse>(API_TOKENS_BASE, {
    method: "POST",
    body: JSON.stringify(req),
  });
}

// Soft-revoke one token. 404 if not found / not owned / already revoked.
export async function revokeApiToken(id: number): Promise<{ ok: boolean; id: number }> {
  return apiFetch<{ ok: boolean; id: number }>(`${API_TOKENS_BASE}/${id}`, {
    method: "DELETE",
  });
}

// Panic button — revoke every active token owned by the caller.
export async function revokeAllApiTokens(): Promise<{ revoked: number }> {
  return apiFetch<{ revoked: number }>(`${API_TOKENS_BASE}/revoke-all`, {
    method: "POST",
  });
}
