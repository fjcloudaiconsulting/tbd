import "server-only";

import { cookies } from "next/headers";
import { cache } from "react";
import { logger } from "./logger";
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
// URL resolution. The browser uses relative URLs and nginx routes them. The
// server (this module) needs an absolute URL to reach the backend. In
// docker-compose dev and prod, BACKEND_INTERNAL_URL points at the backend
// service (`http://backend:8000`). On DO App Platform it resolves to the
// inter-service private URL. Fallback chain ends at `http://localhost:8000`
// so a developer running the backend directly outside docker can still
// import this module and have it work.

const SERVER_API_URL =
  process.env.BACKEND_INTERNAL_URL ||
  process.env.NEXT_PUBLIC_API_URL ||
  "http://localhost:8000";

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

    try {
      const res = await fetch(`${SERVER_API_URL}/api/v1/auth/verify`, {
        method: "POST",
        headers: {
          Cookie: `${refresh.name}=${refresh.value}`,
        },
        // No credentials: 'include' on server-side fetch — we forward the
        // cookie explicitly via the Cookie header above. The endpoint does
        // not issue a Set-Cookie response, so there's nothing to propagate
        // back.
        cache: "no-store",
      });

      if (!res.ok) return null;

      const payload = (await res.json()) as {
        user: User;
        access_token: string;
        token_type: string;
      };
      if (!payload.access_token || !payload.user) return null;

      return { user: payload.user, accessToken: payload.access_token };
    } catch (err) {
      // Transient fetch/JSON failure during RSC render. Examples: DNS race
      // during deploy, connection reset, TLS error, malformed response body.
      // Callers (forecast-plans/page.tsx, reconcile/page.tsx) treat null as
      // "redirect to /login", where AuthProvider's client-side refresh
      // recovers a still-valid session into /dashboard. Without this catch,
      // transient blips surface as the Next.js error boundary with a digest.
      //
      // Sanitization is non-negotiable: log ONLY backend_host, error_name,
      // error_message. Never the refresh cookie value, never any header
      // value, never any token, never any response body.
      logger.warn("server_session_verify_failed", {
        backend_host: new URL(SERVER_API_URL).host,
        error_name: err instanceof Error ? err.name : "Unknown",
        error_message: err instanceof Error ? err.message : String(err),
      });
      return null;
    }
  },
);

export const getServerUser = async (): Promise<User | null> => {
  const session = await getServerSession();
  return session?.user ?? null;
};
