import "server-only";

import { cookies } from "next/headers";
import { cache } from "react";
import { serverFetch } from "./server-fetch";
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
// All transport (rejected fetch / non-OK / invalid JSON / sanitized logging)
// goes through `serverFetch`. A 401 from /auth/verify is part of normal
// auth flow (no refresh cookie → 401), so we pass `silentStatuses: [401]`
// to avoid noisy warns for that one expected case. Real backend outages
// (500/503) still emit `server_fetch_non_ok` so on-call can see them.
// Transient fetch failures still log a sanitized `server_fetch_failed`
// event from inside the helper.

const REFRESH_COOKIE_NAME = "refresh_token";

export type ServerSession = {
  user: User;
  accessToken: string;
};

export const getServerSession = cache(
  async (): Promise<ServerSession | null> => {
    const cookieStore = await cookies();
    const refresh = cookieStore.get(REFRESH_COOKIE_NAME);
    if (!refresh) return null;

    const payload = await serverFetch<{
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
    });

    if (!payload || !payload.access_token || !payload.user) return null;

    return { user: payload.user, accessToken: payload.access_token };
  },
);

export const getServerUser = async (): Promise<User | null> => {
  const session = await getServerSession();
  return session?.user ?? null;
};
