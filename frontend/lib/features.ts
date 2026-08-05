/**
 * Per-org feature flags: the shape, and what a client believes before (or
 * without) an /auth/status answer.
 *
 * Lives in `lib/` rather than beside `useAuth` on purpose. It is DATA, not
 * context, and ~40 test files replace `@/components/auth/AuthProvider`
 * wholesale with a two-key `vi.mock`; a consumer importing a constant from
 * that module gets `undefined` in every one of them and crashes at render.
 */
export type FeatureFlags = {
  reports: boolean;
  plans: boolean;
  customDashboard: boolean;
  forecast: boolean;
  budgets: boolean;
};

/**
 * The single source of truth for the pre-/auth/status state. Three
 * hand-maintained copies of this literal used to exist — the AuthProvider
 * useState init, logout(), and AppShell's `features ?? {...}` fallback — and
 * the polarity is deliberately NOT uniform, which makes drift between copies a
 * guaranteed bug rather than a possible one.
 *
 * reports / plans / customDashboard are OPT-IN rollout flags: showing them
 * before the server confirms would flash a surface the org may not have.
 *
 * forecast / budgets are TABLE STAKES — on for every org, and off only by a
 * deliberate per-org choice. Defaulting them false would flash them MISSING
 * for 100% of orgs on every cold load, to spare the small minority who turned
 * one off a brief flash of a nav item. That is the accepted trade: a flash,
 * not a leak — every route behind them is closed server-side by
 * `require_feature`.
 *
 * Two-state boolean, no tri-state "unknown": every consumer reads
 * `features?.x === false` or plain truthiness, and both collapse a third state
 * back into one of these two, so an "unknown" would be unobservable.
 */
export const DEFAULT_FEATURES: FeatureFlags = {
  reports: false,
  plans: false,
  customDashboard: false,
  forecast: true,
  budgets: true,
};

/**
 * The `features` object as `/api/v1/auth/status` sends it: snake_case, and
 * every key optional because an older API revision may not carry them.
 */
export type AuthStatusFeatures = {
  reports?: boolean;
  plans?: boolean;
  custom_dashboard?: boolean;
  forecast?: boolean;
  budgets?: boolean;
};

/**
 * Wire payload → `FeatureFlags`. The ONLY place the polarity split is applied.
 *
 * `Boolean()` for the opt-in rollout flags; `!== false` for the table-stakes
 * pair, because an absent key means an API revision that predates them and the
 * shipped polarity there is ON — `Boolean(undefined)` would silently close
 * both surfaces for every client during a partial deploy.
 *
 * Extracted because AuthProvider hand-rolled this block THREE times (boot
 * unauthenticated, boot authenticated, `login()`). A mutant that flattened the
 * polarity in the `login()` copy alone survived every test in the diff: the
 * two boot copies kept the suite green while every user who signed in
 * interactively lost Budgets and Forecast. Three copies of a deliberately
 * non-uniform rule is the "half-fix leaves a door" shape — one copy fixed, the
 * others left holding the defect.
 */
export function parseFeatures(
  raw: AuthStatusFeatures | undefined,
): FeatureFlags {
  return {
    reports: Boolean(raw?.reports),
    plans: Boolean(raw?.plans),
    customDashboard: Boolean(raw?.custom_dashboard),
    forecast: raw?.forecast !== false,
    budgets: raw?.budgets !== false,
  };
}
