/**
 * TBD-620 no SWR response cached for one user renders for the next. User A
 * signs out (or the session is ended for them), user B signs in on the same
 * tab, and B's still-pending fetches must not be answered from A's cache.
 * Driven through the REAL AuthProvider and the real apiFetch (only the
 * network is mocked), under a fresh-cache SWRConfig like the app's global one.
 */
import { useEffect, useState } from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import useSWR, { SWRConfig } from "swr";

import { AuthProvider, useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, signInWithAccessToken } from "@/lib/api";
import { useAiStatus } from "@/lib/hooks/use-ai-status";

// vitest.setup.ts stubs this hook out globally; this fence needs the real SWR call.
vi.unmock("@/lib/hooks/use-ai-status");

const b64 = (s: string) => btoa(s).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
const tok = (sub: string, org: number) =>
  `${b64('{"alg":"HS256"}')}.${b64(JSON.stringify({ sub, org_id: org, exp: Math.floor(Date.now() / 1000) + 900 }))}.sig`;
const userA = () => ({ id: 5, org_id: 7, email: "a@x.test" });
const userB = () => ({ id: 9, org_id: 11, email: "b@x.test" });

const json = (status: number, b: unknown) =>
  new Response(JSON.stringify(b), { status, headers: { "content-type": "application/json" } });
// B's data requests never answer, so anything the probe shows for B came from the cache.
const pending = () => new Promise<Response>(() => {});

let refresh: () => Response;
let me: () => Response;
let data: (url: string) => Promise<Response> | Response;
let auth!: ReturnType<typeof useAuth>;
let onLoginPage!: (v: boolean) => void;

beforeEach(() => {
  signInWithAccessToken(null);
  refresh = () => json(200, { access_token: tok("5", 7) });
  me = () => json(200, userA());
  data = (url) => json(200, { owner: "A", url });
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/api/v1/auth/status")) return json(200, { needs_setup: false });
    if (url.includes("/api/v1/auth/refresh")) return refresh();
    if (url.includes("/api/v1/auth/me")) return me();
    if (url.includes("/api/v1/auth/login")) return json(200, { access_token: tok("9", 11) });
    if (url.includes("/api/v1/auth/logout")) return json(200, {});
    if (url.includes("/api/v1/ai/status") || url.includes("/api/v1/agent/usage")) return data(url);
    return json(401, { detail: "no" });
  }));
});

afterEach(() => {
  vi.unstubAllGlobals();
  signInWithAccessToken(null);
});

/** Two SWR-backed surfaces: the real AI status hook and a plain keyed fetch. */
function Probe() {
  const ai = useAiStatus() as unknown as { owner: string } | undefined;
  const { data: usage } = useSWR<{ owner: string }>("/api/v1/agent/usage", (u: string) => apiFetch<{ owner: string }>(u));
  return <div data-testid="probe">{`ai=${ai?.owner ?? "-"} usage=${usage?.owner ?? "-"}`}</div>;
}

function Harness() {
  const ctx = useAuth();
  const [login, setLogin] = useState(false);
  useEffect(() => { auth = ctx; onLoginPage = setLogin; });
  return (
    <>
      <div data-testid="state">{ctx.loading ? "loading" : ctx.user ? String(ctx.user.id) : "none"}</div>
      {/* Like AppShell: the app's SWR surfaces mount only while someone is signed in, off /login. */}
      {ctx.user && !login ? <Probe key={ctx.user.id} /> : null}
    </>
  );
}

const mount = () =>
  render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <AuthProvider><Harness /></AuthProvider>
    </SWRConfig>,
  );
const settled = (who: string) => waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent(who));

/** A is signed in and both surfaces have cached A's responses. */
async function signedInAsAWithCache() {
  mount();
  await settled("5");
  await waitFor(() => expect(screen.getByTestId("probe")).toHaveTextContent("ai=A usage=A"));
}

/** B signs in on /login in the same tab, then lands in the app; nothing cached for A may render for B. */
async function signInAsBSeesNothingOfA() {
  me = () => json(200, userB());
  data = pending;
  act(() => onLoginPage(true));
  await act(async () => { await auth.login("b", "pw"); });
  await settled("9");
  act(() => onLoginPage(false));
  await act(async () => { await new Promise((r) => setTimeout(r, 20)); });
  expect(screen.getByTestId("probe")).toHaveTextContent("ai=- usage=-");
}

describe("TBD-620: the SWR cache does not outlive the signed-in user", () => {
  it("an explicit logout, then B signs in", async () => {
    await signedInAsAWithCache();
    await act(async () => { await auth.logout(); });
    await settled("none");
    await signInAsBSeesNothingOfA();
  });

  it("a forced session end (401 after a failed refresh, through apiFetch), then B signs in", async () => {
    await signedInAsAWithCache();
    refresh = () => json(401, { detail: "no session" });
    me = () => json(401, { detail: "no session" }); // apiFetch's /me probe confirms the death
    await act(async () => { await apiFetch("/api/v1/accounts").catch(() => {}); });
    await settled("none");
    await signInAsBSeesNothingOfA();
  });

  it("B signs in over A without a logout in between (the account switch)", async () => {
    await signedInAsAWithCache();
    await signInAsBSeesNothingOfA();
  });

  it("a same-user refreshMe keeps the cache", async () => {
    await signedInAsAWithCache();
    await act(async () => { await auth.refreshMe(); });
    expect(screen.getByTestId("probe")).toHaveTextContent("ai=A usage=A");
  });
});
