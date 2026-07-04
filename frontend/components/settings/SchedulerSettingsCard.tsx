"use client";

/**
 * SchedulerSettingsCard — org-admin toggles for the scheduled-tasks
 * subsystem (Task 13). Backed by GET/PUT /api/v1/scheduler/settings
 * (Task 3).
 *
 * Three controls, each persisted independently as soon as it changes
 * (no page-level Save button, matching the Manual Balance Adjustment
 * card's optimistic-toggle pattern on this same page):
 *   - automate_recurring_generation: generate recurring transactions
 *     automatically on their due date.
 *   - automate_billing_close: close the billing period automatically
 *     at the cycle boundary. This ALSO gates the pre-close reminder
 *     notification (see backend AUTOMATE_BILLING_KEY) — turning this
 *     off silences both the heads-up and the close itself. That
 *     coupling is intentional per the scheduler spec, not a bug.
 *   - billing_close_reminder_lead_days: how many days before the
 *     close boundary the reminder notification fires (0-31).
 *
 * State is seeded ONCE from GET on mount via a `useEffect([])` — it is
 * deliberately NOT re-seeded from a prop/value on every render. A
 * `useEffect([value])` re-seed would clobber an in-progress edit
 * (see the team's prop-state-reset flake note); this component seeds
 * from its own fetch, not a prop, but keeps the same one-shot
 * discipline for the same reason.
 *
 * Each control updates optimistically then rolls back on a rejected
 * PUT, surfacing an inline error banner (mirrors DemoDataCard).
 */
import { useEffect, useState } from "react";

import { getSchedulerSettings, updateSchedulerSettings, extractErrorMessage } from "@/lib/api";
import { card, cardHeader, cardTitle, label, input } from "@/lib/styles";
import type { SchedulerSettings } from "@/lib/types";

const MIN_LEAD_DAYS = 0;
const MAX_LEAD_DAYS = 31;

type BooleanField = "automate_recurring_generation" | "automate_billing_close";

export default function SchedulerSettingsCard() {
  const [settings, setSettings] = useState<SchedulerSettings | null>(null);
  const [leadDaysDraft, setLeadDaysDraft] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [savingField, setSavingField] = useState<
    BooleanField | "billing_close_reminder_lead_days" | null
  >(null);

  // Seed once on mount. Deliberately NOT re-run on `settings` changes —
  // this is the load, not a resync.
  useEffect(() => {
    let cancelled = false;
    getSchedulerSettings()
      .then((s) => {
        if (cancelled) return;
        setSettings(s);
        setLeadDaysDraft(String(s.billing_close_reminder_lead_days));
      })
      .catch((err) => {
        if (cancelled) return;
        setError(extractErrorMessage(err, "Could not load automatic task settings."));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleToggle(field: BooleanField, next: boolean) {
    if (!settings) return;
    setError(null);
    const prev = settings[field];
    setSettings({ ...settings, [field]: next });
    setSavingField(field);
    try {
      await updateSchedulerSettings({ [field]: next });
    } catch (err) {
      setSettings((current) => (current ? { ...current, [field]: prev } : current));
      setError(extractErrorMessage(err, "Could not update setting."));
    } finally {
      setSavingField(null);
    }
  }

  async function commitLeadDays() {
    if (!settings) return;
    const parsed = Number(leadDaysDraft);
    if (
      !Number.isFinite(parsed) ||
      !Number.isInteger(parsed) ||
      parsed < MIN_LEAD_DAYS ||
      parsed > MAX_LEAD_DAYS
    ) {
      // Invalid draft: revert the field to the last known-good value.
      setLeadDaysDraft(String(settings.billing_close_reminder_lead_days));
      return;
    }
    if (parsed === settings.billing_close_reminder_lead_days) return;

    setError(null);
    const prev = settings.billing_close_reminder_lead_days;
    setSettings({ ...settings, billing_close_reminder_lead_days: parsed });
    setSavingField("billing_close_reminder_lead_days");
    try {
      await updateSchedulerSettings({ billing_close_reminder_lead_days: parsed });
    } catch (err) {
      setSettings((current) =>
        current ? { ...current, billing_close_reminder_lead_days: prev } : current,
      );
      setLeadDaysDraft(String(prev));
      setError(extractErrorMessage(err, "Could not update setting."));
    } finally {
      setSavingField(null);
    }
  }

  return (
    <div className={card} data-testid="settings-scheduler-card">
      <div className={cardHeader}>
        <h2 className={cardTitle}>Automatic tasks</h2>
      </div>
      <div className="p-6 space-y-5">
        <p className="text-sm text-text-secondary">
          Control which scheduled tasks run automatically for your organization.
        </p>
        {error ? (
          <p role="alert" className="text-sm text-danger">
            {error}
          </p>
        ) : null}

        {loading || !settings ? (
          <p className="text-sm text-text-muted">Loading...</p>
        ) : (
          <>
            <div className="flex items-center justify-between gap-4">
              <div>
                <p className="text-sm text-text-primary">
                  Automatically generate recurring transactions
                </p>
                <p className="text-xs text-text-muted">
                  Create each recurring transaction on its due date without manual entry.
                </p>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={settings.automate_recurring_generation}
                aria-label="Automatically generate recurring transactions"
                disabled={savingField === "automate_recurring_generation"}
                onClick={() =>
                  handleToggle(
                    "automate_recurring_generation",
                    !settings.automate_recurring_generation,
                  )
                }
                className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition disabled:opacity-50 ${
                  settings.automate_recurring_generation ? "bg-success" : "bg-border"
                }`}
              >
                <span
                  className={`inline-block h-5 w-5 transform rounded-full bg-white shadow transition ${
                    settings.automate_recurring_generation
                      ? "translate-x-5"
                      : "translate-x-0.5"
                  }`}
                />
              </button>
            </div>

            <div className="flex items-center justify-between gap-4">
              <div>
                <p className="text-sm text-text-primary">
                  Automatically close billing period
                </p>
                <p className="text-xs text-text-muted">
                  Close the period at the cycle boundary and open the next one automatically.
                  Turning this off also silences the pre-close reminder below.
                </p>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={settings.automate_billing_close}
                aria-label="Automatically close billing period"
                disabled={savingField === "automate_billing_close"}
                onClick={() =>
                  handleToggle("automate_billing_close", !settings.automate_billing_close)
                }
                className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition disabled:opacity-50 ${
                  settings.automate_billing_close ? "bg-success" : "bg-border"
                }`}
              >
                <span
                  className={`inline-block h-5 w-5 transform rounded-full bg-white shadow transition ${
                    settings.automate_billing_close ? "translate-x-5" : "translate-x-0.5"
                  }`}
                />
              </button>
            </div>

            <div>
              <label htmlFor="scheduler-lead-days" className={label}>
                Days before close to notify members
              </label>
              <input
                id="scheduler-lead-days"
                type="number"
                min={MIN_LEAD_DAYS}
                max={MAX_LEAD_DAYS}
                inputMode="numeric"
                value={leadDaysDraft}
                disabled={savingField === "billing_close_reminder_lead_days"}
                onChange={(e) => setLeadDaysDraft(e.target.value)}
                onBlur={commitLeadDays}
                className={`${input} w-24`}
                aria-describedby="scheduler-lead-days-hint"
              />
              <p id="scheduler-lead-days-hint" className="mt-1.5 text-xs text-text-muted">
                0 to 31 days. Only sent while automatic close is enabled above.
              </p>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
