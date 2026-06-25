/**
 * Shared tour constants (L5.3 — help residuals).
 *
 * Single source of truth for the dot-namespaced tour step ids and the
 * sessionStorage flag the dashboard auto-start watcher consumes. Pages
 * decorate elements with ``<TourAnchor id="<step-id>">`` and the
 * provider matches them against this list at runtime.
 *
 * Two preset step lists:
 *
 *   - ``DASHBOARD_TOUR_STEPS`` — the short, dashboard-only tour the
 *     first-run wizard offers right after signup. Stays inside one
 *     page, no routing.
 *
 *   - ``EXTENDED_TOUR_STEPS`` — the multi-surface replay tour exposed
 *     via the user menu's "Replay product tour" action. Walks through
 *     the dashboard plus six feature pages (transactions, accounts,
 *     categories, budgets, reports, plans). The provider auto-pushes
 *     the router when the next step's prefix differs from the current
 *     route.
 *
 * Step copy is co-located in ``STEP_COPY`` below so the wizard team
 * can tweak voice without chasing JSX. House style: short sentences,
 * commas/periods over em-dashes, plain words ("see", "shows", "moves"
 * etc.). No AI-isms ("delve", "leverage", "robust").
 */

export const TOUR_FLAG_KEY = "tbd-pending-dashboard-tour";

/** Value written to ``TOUR_FLAG_KEY`` to request the extended replay. */
export const TOUR_FLAG_VALUE_EXTENDED = "extended";
/** Value written for the original first-run dashboard tour. */
export const TOUR_FLAG_VALUE_DASHBOARD = "1";

// Phase 3b: LegacyDashboard removed — only dashboard.header is currently
// wired in CustomDashboard. The remaining step anchors (import-cta,
// period-nav, on-track-tile, account-forecast) need re-wiring in a
// follow-on task before the full first-run tour can be restored.
export const DASHBOARD_TOUR_STEPS = [
  "dashboard.header",
];

/**
 * Extended replay tour. One stop per top-level page so a curious user
 * can revisit the whole product without being overwhelmed. The
 * provider matches the page prefix ("dashboard", "transactions", ...)
 * against the current pathname and routes between steps when the
 * prefix changes.
 */
export const EXTENDED_TOUR_STEPS = [
  "dashboard.header",
  "transactions.title",
  "accounts.title",
  "categories.title",
  "budgets.title",
  "reports.title",
  "plans.title",
];

export interface TourStepCopy {
  title: string;
  body: string;
}

/**
 * Copy for every supported tour step. Used by the overlay card. Keys
 * must match the ``data-tour-id`` attributes the pages set via
 * ``<TourAnchor>``.
 */
export const STEP_COPY: Record<string, TourStepCopy> = {
  "dashboard.header": {
    title: "Welcome to your dashboard",
    body: "This is where you will see how the month is going at a glance. Net cashflow, balances, and what is coming up.",
  },
  "dashboard.import-cta": {
    title: "Bring in your transactions",
    body: "Import a bank export here, or add transactions one by one. The Better Decision works with whatever you have.",
  },
  "dashboard.period-nav": {
    title: "Move through periods",
    body: "Each month is its own billing period. Use these arrows to look back at history or peek ahead.",
  },
  "dashboard.on-track-tile": {
    title: "How the month is shaping up",
    body: "On Track tells you if your spending plan and your reality agree. Green means you are on it. Yellow means it is worth a look.",
  },
  "dashboard.account-forecast": {
    title: "Account forecast",
    body: "We project each account out to the end of the period using your recurring transactions and budgets.",
  },
  "transactions.title": {
    title: "Transactions",
    body: "Every account entry lives here. Add or import them, filter by date or category, and mark transfers so the totals stay clean.",
  },
  "accounts.title": {
    title: "Accounts",
    body: "Track each account you own. Pending entries are kept separate from settled, and the forecast uses both to show where you land.",
  },
  "categories.title": {
    title: "Categories",
    body: "Categories shape your budgets and reports. Masters group spending, subcategories sit underneath, and Edit mode lets you reorder them.",
  },
  "budgets.title": {
    title: "Budgets",
    body: "Set a monthly limit per category. The dashboard tile tells you whether your actual spending is on track or drifting.",
  },
  "reports.title": {
    title: "Reports",
    body: "Build a layout of KPIs and charts over your transactions. Save it, name it, share it across your org.",
  },
  "plans.title": {
    title: "Plans",
    body: "Plans is the sandbox for big one off decisions. House move, career change, sabbatical. Nothing here touches your real transactions.",
  },
};

/**
 * Returns the page-prefix portion of a dot-namespaced step id
 * (e.g. ``"transactions"`` from ``"transactions.title"``). Used by the
 * provider's router effect to decide whether the next step is on a
 * different surface.
 */
export function pagePrefix(stepId: string): string {
  const dot = stepId.indexOf(".");
  return dot >= 0 ? stepId.slice(0, dot) : stepId;
}

/**
 * Maps a step's page prefix to the route the provider should push to
 * before the overlay measures the anchor. Kept tight to the prefixes
 * EXTENDED_TOUR_STEPS uses; unknown prefixes return null and the
 * provider falls back to the current route (the overlay's auto-skip
 * will then advance past missing anchors).
 */
export function routeForPrefix(prefix: string): string | null {
  switch (prefix) {
    case "dashboard":
      return "/dashboard";
    case "transactions":
      return "/transactions";
    case "accounts":
      return "/accounts";
    case "categories":
      return "/categories";
    case "budgets":
      return "/budgets";
    case "reports":
      return "/reports";
    case "plans":
      return "/plans";
    default:
      return null;
  }
}
