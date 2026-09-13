"use client";

import { useRef, useState } from "react";

import { useAuth } from "@/components/auth/AuthProvider";
import Switch from "@/components/ui/Switch";
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

function ToolRow({
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
          <p id={`planning-tool-${tool}-locked`} className="text-xs text-text-muted">
            Off &mdash; set by your administrator
          </p>
        )}
      </div>
      <Switch
        checked={enabled}
        onChange={onToggle}
        label={label}
        pending={saving}
        describedBy={lockedByAdmin ? `planning-tool-${tool}-locked` : undefined}
      />
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
  // Every tool with a save in flight (TBD-323). A SET, not one value: with a
  // single value, finishing Forecast cleared the pending state of a Budgets
  // save still in flight.
  const [saving, setSaving] = useState<ReadonlySet<PlanningTool>>(() => new Set());
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

  // Synchronous re-entry guard (TBD-323), the second line behind the switch's
  // `pending` no-op: this card tracks independent saves per tool, and a second
  // PUT for a tool still in flight must be refused even if rendered state were
  // ever wrong about it. Per tool, so saving Budgets never blocks Forecast.
  const inFlight = useRef(new Set<PlanningTool>());

  async function handleToggle(tool: PlanningTool, next: boolean) {
    if (inFlight.current.has(tool)) return;
    inFlight.current.add(tool);
    setError("");
    setSaving((current) => new Set(current).add(tool));
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
      inFlight.current.delete(tool);
      setSaving((current) => {
        const next = new Set(current);
        next.delete(tool);
        return next;
      });
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
        {error && (
          <div className={errorCls} role="alert">
            {error}
          </div>
        )}
        {tools.map((tool) => (
          <ToolRow
            key={tool}
            tool={tool}
            enabled={isEnabled(tool)}
            saving={saving.has(tool)}
            lockedByAdmin={Boolean(lockedByAdmin[tool])}
            onToggle={(next) => void handleToggle(tool, next)}
          />
        ))}
      </div>
    </div>
  );
}
