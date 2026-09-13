"use client";

/**
 * The app's one on/off switch (TBD-323). Every `role="switch"` lives here;
 * tests/convention/switch-primitive.test.ts fails on a hand-rolled copy.
 *
 * A component, not a lib/styles.ts string: its invariants are ARIA role, state
 * and name, which a class string cannot carry.
 *
 * - `label` is the accessible name: the OBJECT ("Budgets"), never the action,
 *   and never derived from `checked`. `aria-checked` already announces the
 *   state; a name that flips ("Disable Budgets") reads as a different control
 *   and states the value twice in opposite polarities.
 * - `disabled` is LOCKED (real `disabled`, not focusable).
 * - `pending` is a save in flight: `aria-disabled`, still focusable, clicks
 *   ignored. Real `disabled` on a focused button drops keyboard focus to the
 *   page (the HTML focus-fixup rule). A caller tracking several independent
 *   saves keeps its in-flight set in state and also guards re-entry with a
 *   synchronous ref in its handler (SchedulerSettingsCard, PlanningToolsCard).
 * - No focus class. The global `:focus-visible` outline in globals.css
 *   (TBD-319) is the indicator; a 30%-alpha ring measures about 1.78:1.
 * - Colours measured against `surface` ONLY (WCAG 1.4.11, >= 3.30:1 in both
 *   themes and both states): `success` on, `border-strong` off, `surface`
 *   knob, no resting shadow. Do not place a Switch on surface-raised or bg.
 */
export default function Switch({
  checked,
  onChange,
  label,
  disabled = false,
  pending = false,
  describedBy,
  layout = "inline",
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  disabled?: boolean;
  pending?: boolean;
  describedBy?: string;
  layout?: "inline" | "stacked";
}) {
  const stateText = checked ? "Enabled" : "Disabled";
  return (
    <span
      className={
        layout === "stacked"
          ? "inline-flex flex-col items-center"
          : "inline-flex shrink-0 items-center gap-3"
      }
    >
      {layout === "inline" && (
        <span aria-hidden="true" className="text-sm text-text-secondary">
          {stateText}
        </span>
      )}
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={label}
        aria-describedby={describedBy}
        aria-disabled={pending || undefined}
        disabled={disabled}
        onClick={() => {
          if (!pending) onChange(!checked);
        }}
        className="inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-md disabled:cursor-not-allowed disabled:opacity-50 aria-disabled:opacity-50"
      >
        <span
          aria-hidden="true"
          className={`relative block h-6 w-11 rounded-full transition-colors ${
            checked ? "bg-success" : "bg-border-strong"
          }`}
        >
          <span
            className={`absolute top-0.5 left-0 block h-5 w-5 rounded-full bg-surface transition-transform ${
              checked ? "translate-x-[1.375rem]" : "translate-x-0.5"
            }`}
          />
        </span>
      </button>
      {layout === "stacked" && (
        <span aria-hidden="true" className="text-xs text-text-muted">
          {stateText}
        </span>
      )}
    </span>
  );
}
