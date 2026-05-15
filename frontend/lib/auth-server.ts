import "server-only";

import { cookies } from "next/headers";
import { cache } from "react";
import { serverFetchResult } from "./server-fetch";
import type { User } from "./types";

// Foundation for Server Component migrations. The client-side `apiFetch` in
// lib/api.ts cannot run in an RSC: the access token lives in an in-memory
// module variable, and `next/headers` cookies are only available server-side.
//
// This module reads the refresh cookie that the backend sets at login time
// (`refresh_token`, HTTP-only, Path=/) and forwards it to a purpose-built
// backend endpoint that validates the cookie WITHOUT rotation, then returns
// `{ user, access_token, token_type }` in one round-trip. See backend PR #211
// for the endpoint contract and the latent FastAPI cookie-merge bug it
// documented along the way.
//
// Results are cached per request via React's `cache` so multiple RSCs in a
// single render only pay the network cost once.
//
// All transport (rejected fetch / non-OK / invalid JSON / timeout /
// sanitized logging) goes through `serverFetchResult`, which returns a
// discriminated result so this module can distinguish:
//
//   - 401 / 403          → unauthenticated (refresh cookie invalid or
//                          missing on the backend; redirect to /login).
//   - timeout / abort /  → transient (backend wedged or unreachable;
//     network_error /     callers should render a recoverable fallback
//     invalid_json /      shell rather than redirect to /login, so a
//     5xx                 transient outage doesn't false-logout a user).
//
// This taxonomy is the fix for PR #288: before, ANY non-200 from
// /auth/verify (including timeout and 5xx) was treated as "no session"
// and the page redirected to /login, OR the request hung forever and
// the page stayed on loading.tsx indefinitely. Neither is correct.
// 401 from /auth/verify is part of normal auth flow (no refresh cookie
// validated → 401), so we pass `silentStatuses: [401]` to avoid noisy
// warns for that one expected case. Real backend outages (500/503) and
// timeouts still emit `server_fetch_non_ok` / `server_fetch_failed` so
// on-call can see them.

const REFRESH_COOKIE_NAME = "refresh_token";

// Per-call timeout for the verify hot path. The default 5s budget in
// `serverFetch` is fine; we set it explicitly here so the value is
// reviewable next to the auth flow it gates. If we ever want a tighter
// budget for verify specifically, this is the dial.
const VERIFY_TIMEOUT_MS = 5_000;

export type ServerSession = {
  user: User;
  accessToken: string;
};

// Discriminated result so RSC callers can triage transient backend
// trouble (timeout / 5xx / network error) without false-logout. The
// "transient" branch carries a coarse-grained reason so callers can
// surface different fallback copy if they choose; today all transient
// reasons render the same "data unavailable, retrying" empty shell.
export type ServerSessionResult =
  | { kind: "authenticated"; session: ServerSession }
  | { kind: "unauthenticated" }
  | {
      kind: "transient";
      reason: "timeout" | "network" | "server_error" | "invalid_payload";
    };

export const getServerSessionResult = cache(
  async (): Promise<ServerSessionResult> => {
    const cookieStore = await cookies();
    const refresh = cookieStore.get(REFRESH_COOKIE_NAME);
    if (!refresh) return { kind: "unauthenticated" };

    const result = await serverFetchResult<{
      user: User;
      access_token: string;
      token_type: string;
    }>("/api/v1/auth/verify", {
      method: "POST",
      cookie: `${refresh.name}=${refresh.value}`,
      // 401 here means "no session", not an outage. Suppress warn-level
      // logging for that exact status; rejected-fetch, invalid-JSON, and
      // other non-OK statuses (e.g. 500/503) still log.
      silentStatuses: [401],
      timeoutMs: VERIFY_TIMEOUT_MS,
    });

    switch (result.kind) {
      case "ok": {
        const payload = result.data;
        if (!payload || !payload.access_token || !payload.user) {
          return { kind: "transient", reason: "invalid_payload" };
        }
        return {
          kind: "authenticated",
          session: {
            user: payload.user,
            accessToken: payload.access_token,
          },
        };
      }
      case "http_error":
        // 401/403 = no session. Anything else (5xx, unexpected 4xx) is
        // a backend signal we cannot interpret as auth death without
        // risking false-logout during an outage.
        if (result.status === 401 || result.status === 403) {
          return { kind: "unauthenticated" };
        }
        return { kind: "transient", reason: "server_error" };
      case "timeout":
      case "abort":
        return { kind: "transient", reason: "timeout" };
      case "network_error":
        return { kind: "transient", reason: "network" };
      case "invalid_json":
        return { kind: "transient", reason: "invalid_payload" };
    }
  },
);

// Pre-launch state means no deprecation shims (see CLAUDE.md). All RSC
// callers were migrated to `getServerSessionResult` in PR #288. If you
// need a "session-or-null" shape, switch on `result.kind` at the call
// site so transient (timeout / 5xx) doesn't collapse into the same path
// as unauthenticated.
