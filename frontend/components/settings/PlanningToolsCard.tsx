"use client";

import { useState } from "react";

import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { card, cardHeader, cardTitle, error as errorCls } from "@/lib/styles";

/**
 * "Planning tools" — the org's own switches for Forecast and Budgets (TBD-197).
 *
 * Deliberately NOT titled "Features". That word is taken on the ADMIN surface
 * by OrgFeatureGateCard / FeatureOverridesCard, which express platform
 * entitlement; this card expresses tenant preference. Conflating the two is the
 * fault line this ticket's earlier designs kept falling across.
 *
 * The switches mutate immediately with no confirm dialog: nothing is deleted,
 * and re-enabling restores everything. A ConfirmModal on a non-destructive
 * toggle is worse UX than a new switch pattern on this page.
 */
type PlanningTool = "forecast" | "budgets";

const TOOL_LABEL: Record<PlanningTool, string> = {
  forecast: "Forecast",
  budgets: "Budgets",
};

function Switch({
  tool,
  enabled,
  saving,
  lockedByAdmin,
  onToggle,
}: {
  tool: PlanningTool;
  enabled: boolean;
  saving: boolean;
  lockedByAdmin: boolean;
  onToggle: (next: boolean) => void;
}) {
  const label = TOOL_LABEL[tool];
  return (
    // Per-tool container id. With two switches the card carries two
    // "Enabled"/"Disabled" spans and two `role="switch"` nodes, so every test
    // query has to scope to ONE row. Never by positional index (TBD-313) —
    // `getByRole("switch")` matches seven-plus nodes on this page.
    <div
      className="flex items-center justify-between gap-4"
      data-testid={`planning-tool-${tool}`}
    >
      <div>
        <p className="text-sm font-medium text-text-primary">{label}</p>
        {lockedByAdmin && (
          <p className="text-xs text-text-muted">
            Off &mdash; set by your administrator
          </p>
        )}
      </div>
      <div className="flex items-center gap-3">
        {/* State in TEXT, never colour alone. */}
        <span className="text-sm text-text-secondary">
          {enabled ? "Enabled" : "Disabled"}
        </span>
        <button
          type="button"
          role="switch"
          aria-checked={enabled}
          // The accessible name is the OBJECT, never the action. `role="switch"`
          // already announces the state through aria-checked, so an
          // action-phrased name that flips on every toggle ("Disable Budgets" →
          // "Enable Budgets") makes a screen reader read a control that appears
          // to have become a different control, and states it twice — once as
          // the name, once as the checked state, in opposite polarities.
          aria-label={label}
          disabled={saving}
          onClick={() => onToggle(!enabled)}
          // 44px hit area via padding on an h-11 box; the visible track stays
          // small. `h-6 w-11` alone is 24px, under the DESIGN.md touch floor.
          className="inline-flex h-11 w-11 items-center justify-center rounded-md focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30 disabled:opacity-50"
        >
          <span
            aria-hidden="true"
            // `bg-success`/`bg-border`, matching the six existing switches
            // (SchedulerSettingsCard, SmartRulesSection, notifications), four of
            // which render on THIS page. Deliberately NOT `bg-accent`: the switch
            // form was chosen over the neighbouring aria-pressed button precisely
            // to keep this control off the brass budget, and both tools default
            // to enabled, so a brass track would be lit at rest on every load —
            // a sixth accent moment on a page already carrying five btnPrimary,
            // against The One Brass Rule's "at most twice, ideally once".
            className={`relative block h-6 w-11 rounded-full transition-colors ${
              enabled ? "bg-success" : "bg-border"
            }`}
          >
            {/* Knob fill is the `surface` THEME TOKEN, deliberately — the raw
                Tailwind `bg-white` renders identically here and passes CI
                (the design-token gate only rejects raw foreground colours),
                but it does not theme-switch and so violates No Off-Token.
                No resting shadow either (State-Only Shadow Rule). */}
            <span
              className={`absolute top-0.5 block h-5 w-5 rounded-full bg-surface transition-transform ${
                enabled ? "translate-x-[1.375rem]" : "translate-x-0.5"
              }`}
            />
          </span>
        </button>
      </div>
    </div>
  );
}

export default function PlanningToolsCard({
  tools = ["forecast", "budgets"],
}: {
  tools?: PlanningTool[];
}) {
  const { features, refreshFeatures } = useAuth();
  // Local state seeded lazily from the auth context rather than synced through
  // an effect: a prop-to-state reset effect here would fight the write echo
  // (and this repo has a documented flake class for exactly that shape).
  const [written, setWritten] = useState<Partial<Record<PlanningTool, boolean>>>(
    {},
  );
  const [saving, setSaving] = useState<PlanningTool | null>(null);
  // "Off — set by your administrator" is WRITE-RESPONSE-ONLY. /auth/status
  // returns a single resolved boolean, so a global "off" and an org opt-out
  // are indistinguishable at page load; the only moment the difference becomes
  // observable is a PUT {enabled:true} that comes back enabled:false.
  const [lockedByAdmin, setLockedByAdmin] = useState<
    Partial<Record<PlanningTool, boolean>>
  >({});
  const [error, setError] = useState("");

  const isEnabled = (tool: PlanningTool) =>
    written[tool] ?? features?.[tool] !== false;

  async function handleToggle(tool: PlanningTool, next: boolean) {
    setError("");
    setSaving(tool);
    try {
      const res = await apiFetch<{ feature: PlanningTool; enabled: boolean }>(
        `/api/v1/settings/features/${tool}`,
        { method: "PUT", body: JSON.stringify({ enabled: next }) },
      );
      const effective = res?.enabled ?? next;
      setWritten((w) => ({ ...w, [tool]: effective }));
      setLockedByAdmin((l) => ({ ...l, [tool]: next && !effective }));
      // Push the new answer into the auth context, which is where the REST of
      // the app reads it: the nav filter, the page notices, the dashboard fetch
      // skips. AuthProvider resolves `features` only on boot and at login and
      // never unmounts on a client-side navigation, so without this the admin
      // who just switched Budgets off keeps a stale `budgets: true` for the
      // whole session — the nav entry survives, its page 404s into an error
      // banner instead of the notice, and the legacy dashboard's
      // `/api/v1/budgets` fetch rejects inside a `Promise.all` and paints
      // "Failed to load dashboard data" over a deliberate setting. That is the
      // exact failure the fetch skip exists to prevent, reached through the
      // happy path.
      //
      // Best-effort and AFTER the local echo: the write already succeeded, so a
      // failed re-resolve must not read as a failed toggle. The user loses only
      // the cross-surface refresh until the next full load.
      try {
        await refreshFeatures?.();
      } catch {
        // Non-fatal — see above.
      }
    } catch (err) {
      setError(extractErrorMessage(err));
    } finally {
      setSaving(null);
    }
  }

  return (
    <div className={card} data-testid="planning-tools-card">
      <div className={cardHeader}>
        <h2 className={cardTitle}>Planning tools</h2>
      </div>
      <div className="space-y-4 p-6">
        <p className="text-sm text-text-secondary">
          Turn off the parts of the app your household doesn&apos;t use. Nothing
          is deleted, and turning a tool back on restores everything.
        </p>
        {error && <div className={errorCls}>{error}</div>}
        {tools.map((tool) => (
          <Switch
            key={tool}
            tool={tool}
            enabled={isEnabled(tool)}
            saving={saving === tool}
            lockedByAdmin={Boolean(lockedByAdmin[tool])}
            onToggle={(next) => void handleToggle(tool, next)}
          />
        ))}
      </div>
    </div>
  );
}
