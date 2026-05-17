# AuthProvider discriminated mode machine

Status: DESIGN (Team G, 2026-05-17). Implementation lands after Team E (Google SSO disclosure) and Team F (frontend auth P0) merge, to avoid AuthProvider conflicts.

## 1. Current state shape

File: `frontend/components/auth/AuthProvider.tsx` (post `b0bffdd`).

```ts
interface AuthContextValue {           // lines 20-35
  user: User | null;                   // line 21
  loading: boolean;                    // line 22
  needsSetup: boolean;                 // line 23
  login: (...) => Promise<void>;       // line 24
  register: (...) => Promise<void>;    // line 25
  logout: () => Promise<void>;         // line 33
  refreshMe: () => Promise<void>;      // line 34
}
```

Internal `useState` triplet (lines 40-42):

```ts
const [user, setUser] = useState<User | null>(null);
const [loading, setLoading] = useState(true);
const [needsSetup, setNeedsSetup] = useState(false);
```

Semantic ambiguity these three booleans / scalars produce today:

1. `loading=true, user=null, needsSetup=false` overloads two real situations:
   a. Provider just mounted, refresh has not been attempted yet (`setup`).
   b. Refresh is in flight (`restoring`).
2. `loading=false, user=null, needsSetup=false` overloads two real situations:
   a. Genuinely unauthenticated (`unauthenticated`, redirect to /login).
   b. Logged-out via explicit `logout()` vs. session-expired vs. never-authenticated — the `auth:unauthenticated` event handler (lines 84-91) collapses all three to the same imperative state. Consumers cannot tell "you were just logged out" from "your session expired".

   (Note: the *transient refresh failure* path that #287 fixed is **not** in this overload set any more. Team F's PR #299 absorbs transient outcomes entirely inside `api.ts` — `refreshAccessTokenOnce`'s retry budget runs to completion before the provider sees anything, and `apiFetch` only dispatches `auth:unauthenticated` on a terminal outcome that has been re-verified against `/auth/me`. The provider therefore only ever observes "still authenticated" or "definitively unauthenticated"; the transient window is invisible to it. This spec used to model that window as a fourth mode; PR #299 made that unnecessary.)
3. `needsSetup` is independent of `user`/`loading` in the type, but in practice only `needsSetup=true, user=null, loading=false` is meaningful; the other 7 combinations are unreachable yet not enforced.
4. The `auth:unauthenticated` event handler (lines 84-91) collapses session state imperatively; there is no record of whether the collapse came from a terminal refresh failure (api.ts line 199) or from `logout()` (lines 131-139). Consumers cannot tell "you were just logged out" from "your session expired".

## 2. Proposed discriminated mode model

```ts
// Discriminator: AuthMode["mode"] is the source of truth.
// Every other field is only present in the variants where it is meaningful.
export type AuthMode =
  // Pre-hydration. The provider has mounted but has not yet attempted
  // any network call. Render-blocking; nothing should redirect off this.
  | { mode: "setup" }

  // Silent refresh + /me in flight on mount, or /status check still
  // pending. Renders the same shell-spinner UI as `setup` today.
  | { mode: "restoring" }

  // First-app-startup: backend's /api/v1/auth/status returned
  // { needs_setup: true }. /setup is the only legal route in this mode.
  | { mode: "needs_setup" }

  // Logged in, access token in memory, user object loaded.
  | { mode: "authenticated"; user: User }

  // Confirmed no session. The ONLY mode that triggers redirect to /login.
  // Reached via: terminal refresh failure, explicit logout, or initial
  // refresh attempt completed with a clean negative.
  | { mode: "unauthenticated"; reason: "logout" | "expired" | "never_authenticated" };
```

**5-mode machine, not 6.** Earlier drafts of this spec included a
sixth `transient_error` mode that the provider would enter while
`api.ts` was retrying a refresh. PR #299 (Team F) made that
unnecessary: `api.ts` now contains the full transient-recovery
state machine (`refreshAccessTokenOnce` + singleflight + 31s `/me`
liveness re-check), and only dispatches `auth:unauthenticated` when
the outcome is definitively terminal. The provider therefore never
needs to model the in-recovery window. If we later decide we want
non-blocking UI feedback during recovery (e.g. a passive "Reconnecting…"
banner), that is a separate design — `auth:recovery-started` /
`auth:recovery-ended` events with their own consumer set — and
explicitly out of scope here.

**`sessionId` removed from `authenticated`.** Earlier drafts carried
`sessionId: string` alongside `user`. The frontend has no stable
session id available: `/auth/me` does not return one today, and
PR #301's backend `sid` claim lives inside the refresh cookie which
is `HttpOnly` and never reaches JS. Adding the field forced the
implementation to invent or expose something the user could not
observe anyway. Removed; if a future feature genuinely needs a
session id in the frontend, we re-introduce it once the backend
exposes one in the `/me` payload.

### Optional sub-state: SSO first-run disclosure (Team E)

Two viable shapes; Team G's recommendation is **B (user metadata)**:

- **A. Mode sub-state:** add `| { mode: "awaiting_disclosure"; user: User }` between `authenticated` and any consumer that gates on `user`. Pro: forces every consumer to acknowledge the disclosure. Con: every existing route component (~35 callers) needs a new branch; blast radius is exactly the wrong shape for a one-screen modal.
- **B. User-metadata flag (recommended):** Team E adds `User.requires_sso_disclosure: boolean` (or a deterministic computed boolean) to the `/me` payload. AuthProvider stays in `authenticated` mode; an island component near the top of AppShell renders the disclosure modal when the flag is true and clears it via a one-shot mutation. This is the same pattern `onboarded_at` already uses (AppShell.tsx lines 161-168) and the same pattern `email_verified` uses on the gate banner. Blast radius is one new component, not the whole consumer set.

Decision should be confirmed with Team E during their PR review; this spec assumes **B**.

## 3. State transitions

Legal transitions (`from -> trigger -> to`):

| From | Trigger | To |
| --- | --- | --- |
| `setup` | provider effect runs (mount) | `restoring` |
| `restoring` | `/auth/status` returns `needs_setup=true` | `needs_setup` |
| `restoring` | `/auth/refresh` returns `RefreshResult { ok: true }` + `/me` succeeds | `authenticated` |
| `restoring` | `/auth/refresh` returns `RefreshResult { ok: false, kind: "terminal" }` | `unauthenticated { reason: "never_authenticated" }` |
| `restoring` | `/me` succeeds after refresh | `authenticated` |
| `restoring` | `/me` 401 + retry refresh terminal | `unauthenticated { reason: "expired" }` |
| `needs_setup` | `register()` + `login()` succeed | `authenticated` |
| `authenticated` | `apiFetch` 401 + refresh terminal (event `auth:unauthenticated`) | `unauthenticated { reason: "expired" }` |
| `authenticated` | `logout()` resolves (or fails best-effort) | `unauthenticated { reason: "logout" }` |
| `authenticated` | `refreshMe()` 401 + refresh terminal | `unauthenticated { reason: "expired" }` |
| `unauthenticated` | `login()` succeeds | `authenticated` |
| `unauthenticated` | `register()` succeeds (followed by login) | `authenticated` |

Transient refresh outcomes do NOT appear in this table: PR #299
absorbs them inside `api.ts`, so the provider only sees the terminal
verdict (or never sees a refresh event at all if recovery succeeds).
See Section 4 for the `api.ts` contract.

Illegal transitions (the reducer asserts and a structured warning is logged; do not throw, do not silently no-op):

| From | Trigger | Why illegal |
| --- | --- | --- |
| `setup` | any token / user mutation | We have not attempted hydration; ignore. |
| `needs_setup` | `auth:unauthenticated` event | Pre-bootstrap; nobody to log out. |
| `unauthenticated` | `auth:unauthenticated` event | Already terminal. No-op, no log. |
| `authenticated` | `setMode("setup")` or `setMode("restoring")` | Only logout/refresh failure may demote; never silently restart. |

## 4. Integration contract with Team F's api.ts changes

`frontend/lib/api.ts` (as shipped in PR #299) owns the entire transient-recovery state machine: `refreshAccessTokenOnce` singleflight, retry budget, and the 31s `/auth/me` liveness re-check that distinguishes "actually terminal" from "looked terminal during a Redis blip". The provider's job is to subscribe ONLY to the terminal signal that `api.ts` is willing to emit.

Two dispatch sites to wire:

1. **Terminal refresh inside `apiFetch`** (PR #299's terminal branch — final outcome after `refreshAccessTokenOnce` AND `/me` liveness re-check both fail):

   ```ts
   } else if (refreshResult.kind === "terminal") {
     ...
     if (!isCredCheck) {
       accessToken = null;
       if (typeof window !== "undefined") {
         window.dispatchEvent(new CustomEvent("auth:unauthenticated", {
           detail: { reason: "expired" }
         }));
       }
     }
   ```

   Provider listener (today AuthProvider.tsx lines 84-91) maps this to `{ mode: "unauthenticated", reason: "expired" }`. We extend the event to carry a `CustomEvent<{ reason: "expired" }>` payload so the listener does not have to guess, and so `logout()` can dispatch the same event with `reason: "logout"` for a single code path.

2. **Logout** (AuthProvider.tsx lines 131-139): same `auth:unauthenticated` event with `reason: "logout"`. Today logout mutates state directly; we route it through the reducer for symmetry.

There is NO third dispatch site for transient outcomes. PR #299 deliberately keeps transient handling internal to `api.ts`: a transient `RefreshResult { ok: false, kind: "transient" }` causes `apiFetch` to throw `ApiResponseError(503, ...)` so the caller (SWR / page) can render its own retry UI, but no `auth:*` event is dispatched. The provider therefore never observes the transient state.

Failure mode matrix:

| `apiFetch` outcome | Event dispatched | Provider mode |
| --- | --- | --- |
| Success (any 2xx) | none | unchanged |
| 401 -> refresh ok -> retry ok | none | `authenticated` (unchanged) |
| 401 -> refresh ok -> retry 4xx/5xx | none (caller handles) | `authenticated` (unchanged) |
| 401 -> refresh terminal (post `/me` liveness re-check) | `auth:unauthenticated { reason: "expired" }` | `unauthenticated` |
| 401 -> refresh transient (still recovering) | none (503 thrown to caller; provider untouched) | unchanged |
| Network error on primary request (no 401) | none | unchanged (caller's problem) |

## 5. Integration contract with Team E's first-run SSO disclosure

Per section 2 the recommended shape is a User-metadata flag, not a mode sub-state. The contract is:

1. Backend `/api/v1/auth/me` adds a boolean (working name `requires_sso_disclosure`).
2. The `User` type in `frontend/lib/types.ts` gains the boolean (optional, with the same forward-compat semantics as `permissions?` and `onboarded_at?`).
3. AuthProvider does not look at this field. It stays in `authenticated`.
4. A new island component (`SsoDisclosureGate`, Team E's deliverable) mounted near `AppShell`'s root reads `user.requires_sso_disclosure`, renders the modal when true, and on accept calls a new `POST /api/v1/auth/sso/disclosure/ack` followed by `refreshMe()` to clear the flag.
5. The disclosure is non-blocking with respect to mode transitions; e.g. a terminal `auth:unauthenticated` event during the disclosure flips the provider into `unauthenticated`, the modal unmounts with AppShell, and the user lands on `/login`. (PR #299 absorbs transient outcomes internally, so the modal does not need to model a transient sub-state.)

If Team E pushes back and wants the disclosure to be modal-blocking with respect to other auth events, we re-open this and move to **A (mode sub-state)**. We do not commit to either now beyond "the mode machine does not own it".

## 6. Backwards-compat plan

### Existing `useAuth()` consumers (38 sites across 35 distinct files)

Pages / app-router entry points that read `{ user, loading }`:

- `frontend/app/dashboard/page.tsx:103`
- `frontend/app/transactions/page.tsx:69`
- `frontend/app/accounts/page.tsx:20`
- `frontend/app/budgets/page.tsx:21`
- `frontend/app/categories/page.tsx:80`
- `frontend/app/recurring/page.tsx:24`
- `frontend/app/mfa-verify/page.tsx:26`
- `frontend/app/admin/page.tsx:268`
- `frontend/app/admin/roles/page.tsx:214`
- `frontend/app/admin/roles/[id]/page.tsx:31`
- `frontend/app/admin/orgs/page.tsx:45`
- `frontend/app/admin/orgs/[id]/page.tsx:76`
- `frontend/app/admin/subscriptions/page.tsx:172`
- `frontend/app/admin/subscriptions/[id]/page.tsx:110`
- `frontend/app/admin/audit/page.tsx:37`
- `frontend/app/admin/users/page.tsx:114`
- `frontend/app/admin/users/[user_id]/page.tsx:73`
- `frontend/app/admin/analytics/page.tsx:113`
- `frontend/app/system/page.tsx:33`
- `frontend/app/system/plans/page.tsx:40`

Pages that read `{ user, refreshMe }` only:

- `frontend/app/settings/page.tsx:37`
- `frontend/app/auth/google/callback/page.tsx:10`
- `frontend/app/verify-email/page.tsx:14`

Pages that read `{ user, loading, refreshMe }`:

- `frontend/app/settings/organization/page.tsx:37`
- `frontend/app/settings/billing/page.tsx:23`

Page that reads `{ user, login, refreshMe }`:

- `frontend/app/settings/security/page.tsx:64`

Page that reads `{ needsSetup, loading, register, login }`:

- `frontend/app/setup/page.tsx:17`

Layout / shell components:

- `frontend/components/AppShell.tsx:142` reads `{ user, loading, logout }` and owns the `/login` redirect (line 152).
- `frontend/components/SettingsLayout.tsx:17` reads `{ user, loading }`.

Other components reading `{ user, loading, needsSetup }`:

- `frontend/components/auth/LoginPageBody.tsx:39` reads `{ user, login, loading, needsSetup }`.
- `frontend/components/auth/RegisterPageBody.tsx:26` reads `{ user, register, loading }`.
- `frontend/components/landing/LandingAuthRedirect.tsx:12` reads `{ user, loading, needsSetup }`.

Components reading `{ refreshMe }` only:

- `frontend/components/auth/AcceptInviteBody.tsx:25`
- `frontend/components/settings/RestartTourCard.tsx:32`

Components reading `{ user }` only:

- `frontend/components/feedback/FeedbackTrigger.tsx:20`

Onboarding entry point reading `{ user, loading, refreshMe }`:

- `frontend/components/onboarding/OnboardingPageBody.tsx:58`

Out-of-band:

- `frontend/components/auth/AuthProviderApex.tsx:23` is the apex-build stub; mirror the new context value shape (return `{ mode: "unauthenticated", reason: "never_authenticated", login, register, logout, refreshMe, user: null, loading: false, needsSetup: false }`) so the apex bundle keeps type-checking.

### Migration shape: additive in PR 1

The cheapest migration is **additive**: introduce `mode` as a new field on `AuthContextValue` while keeping `user`, `loading`, `needsSetup` as before. The reducer maintains both; consumers move one at a time. Concretely:

```ts
interface AuthContextValue {
  // Existing, derived from `mode`, kept stable for callers
  user: User | null;
  loading: boolean;
  needsSetup: boolean;

  // New: discriminated mode (preferred for new callers, AppShell, login,
  // landing, setup)
  mode: AuthMode;

  login: (...) => Promise<void>;
  register: (...) => Promise<void>;
  logout: () => Promise<void>;
  refreshMe: () => Promise<void>;
}
```

Derivation rules in the provider:

- `user = mode.mode === "authenticated" ? mode.user : null`
- `loading = mode.mode === "setup" || mode.mode === "restoring"`
- `needsSetup = mode.mode === "needs_setup"`

This is what lets us migrate AppShell / LandingAuthRedirect / LoginPageBody / setup / RegisterPageBody (the eight `needsSetup`/redirect-driving consumers) without touching the other 27.

## 7. Test plan

### Unit tests for the reducer

Pure-function reducer with table-driven cases:

1. Each row in section 3's legal-transitions table -> one test asserting `reducer(prev, action)` returns the expected mode.
2. Each row in section 3's illegal-transitions table -> one test asserting `reducer(prev, action) === prev` and that `console.warn` (or `structlog`-flavoured logger if FE has one wired) is called once with a structured payload.
3. Round-trip: starting in `setup`, fire the canonical happy-path sequence (`MOUNT`, `STATUS_OK_AUTHED`, `REFRESH_OK`, `ME_OK { user }`) and assert terminal mode is `authenticated`.
4. Round-trip: starting in `setup`, fire the canonical needs-setup sequence (`MOUNT`, `STATUS_NEEDS_SETUP`) and assert terminal mode is `needs_setup`.

### Integration tests at the consumer level

Use `frontend/tests/components/landing/LandingAuthRedirect.test.tsx` as the template; add cases against the new mode field. New tests in `frontend/tests/components/auth/AuthProvider.test.tsx`:

1. AppShell does NOT redirect while `api.ts` is mid-recovery (`apiFetch` throws 503 with code `refresh_transient`, no `auth:*` event fires, provider stays in `authenticated`). Regression guard for the false-logout class #287 fixed at the api.ts layer — pin shape: render AppShell with provider in `authenticated`, mock `apiFetch` to throw a 503 with `code: "refresh_transient"`, assert no `router.push("/login")` call.
2. AppShell DOES redirect when mode is `unauthenticated` (event `auth:unauthenticated { reason: "expired" }` dispatched after PR #299's terminal `/me` liveness re-check fails).
3. LandingAuthRedirect routes to `/setup` when mode is `needs_setup`, to `/dashboard` when mode is `authenticated`, and stays on landing when mode is `restoring` or `setup` (no flash-of-redirect).
4. `auth:unauthenticated { reason: "expired" }` event flips mode to `unauthenticated` and clears the cached user.
5. `logout()` flips mode to `unauthenticated { reason: "logout" }`.

### Type-level tests

Add a `frontend/tests/types/auth-mode.test-d.ts` (or inline in the unit-test file) that exhaustively switches on `mode.mode` and asserts the discriminated union narrows correctly (`@ts-expect-error` on the wrong field per branch).

## 8. Implementation phasing

**Recommendation: 2 PRs, not 1 and not 3.**

- **PR 1 (Team G implementation PR, after E + F merge):** introduce the reducer + `AuthMode` type, expose `mode` alongside the existing `user/loading/needsSetup` fields, extend the existing `auth:unauthenticated` listener to read the `CustomEvent` `reason` payload, route logout through the reducer, mirror the new shape in `AuthProviderApex.tsx`. Migrate the four consumers whose bugs motivated this work: AppShell (must not redirect on a 503 `refresh_transient` thrown by `apiFetch`), LandingAuthRedirect (cleaner setup-vs-authenticated split), LoginPageBody, setup/page.tsx. Leave the other 31 consumers on the legacy fields. NB: this PR does NOT add any new `auth:*` event — PR #299 already shipped the only one this design needs.

- **PR 2 (a few days later, after PR 1 has run in production for one full deploy cycle):** migrate the remaining 31 consumers; remove `loading`, `needsSetup`, and the public `user` field on `AuthContextValue` (or keep `user` as a stable derived alias indefinitely; the savings from removing it are tiny and the churn is large). Codemod-style PR.

A 3-PR split (introduce -> migrate -> delete booleans) is overkill given the consumer set is well-enumerated and `tsc --noEmit` catches every leftover.

A 1-PR split is rejected because it touches 35 files and is impossible to roll back cleanly if a consumer branch has the wrong narrowing.

## 9. Risks

1. **Terminal-event coupling.** The provider learns about session termination through a single window event dispatched from `api.ts` (PR #299) plus `logout()`. If a future refresh path is added that does NOT use `apiFetch`'s terminal branch, the provider will silently miss the termination and the user stays in `authenticated` against a dead session. Mitigation: Team G's implementation PR introduces a small `emitAuthEvent` helper in `api.ts` (centralising the `dispatchEvent(new CustomEvent("auth:unauthenticated", ...))` calls already shipped by PR #299) and adds a convention test that greps for `dispatchEvent.*auth:` outside that helper.

2. **SSR / hydration.** AuthProvider is a client component (`"use client"`), but `mode: "setup"` is the SSR-rendered value. Any page that uses `mode` server-side will see `setup` and render the spinner, which is the same behavior `loading=true` gives today. No regression, but worth a hydration-mismatch test on the landing route.

3. **Apex stub drift.** `AuthProviderApex.tsx` already drifts (its return shape includes `refresh` not `refreshMe`, see line 37). The mode-machine PR must update the stub to mirror the new shape exactly, and a CI check should `tsc --noEmit -p next.config.apex.ts` to catch future drift.

4. **Team E timing.** If Team E lands `requires_sso_disclosure` before Team G's implementation PR, Team G's PR pulls in the type. If after, Team E adds a one-line field to `User`. Either order works; the mode machine does not block on it.

5. **Future recovery UI.** If product later wants a passive "Reconnecting…" banner during the `api.ts` recovery window, this design does NOT support it as-is — the provider has no signal that recovery is in progress. The follow-up would add `auth:recovery-started` / `auth:recovery-ended` events from `api.ts` and a `isRecovering: boolean` derived value on the context; the mode union stays 5-wide. Not in scope here.
