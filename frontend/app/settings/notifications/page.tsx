"use client";

import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import SettingsLayout from "@/components/SettingsLayout";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import {
  card,
  cardTitle,
  btnPrimary,
  error as errorCls,
  success as successCls,
} from "@/lib/styles";
import type { NotificationPreferences } from "@/lib/types";

type PrefKey = keyof NotificationPreferences;

/**
 * The four notification categories, in display order, each with its
 * email and in-app channel keys. `security` is listed first and is
 * locked on for BOTH channels: the backend rejects `email_security=false`
 * (code=security_emails_required) and force-coerces `in_app_security` to
 * true on read + write, so both switches render disabled+on.
 *
 * The PUT replaces every toggle at once, so the page round-trips the full
 * eight-field shape and `toggle` can flip any single key.
 */
const CATEGORIES: ReadonlyArray<{
  id: string;
  title: string;
  description: string;
  emailKey: PrefKey;
  inAppKey: PrefKey;
  locked?: boolean;
}> = [
  {
    id: "security",
    title: "Security",
    description:
      "Sign-in alerts, password changes, and other account-protection events. Always on so you never miss a security warning.",
    emailKey: "email_security",
    inAppKey: "in_app_security",
    locked: true,
  },
  {
    id: "account",
    title: "Account",
    description:
      "Updates about your own account, like email verification and profile changes.",
    emailKey: "email_account",
    inAppKey: "in_app_account",
  },
  {
    id: "org_admin",
    title: "Organization (admin)",
    description:
      "Administrative events for your organization, such as member invites and role changes.",
    emailKey: "email_org_admin",
    inAppKey: "in_app_org_admin",
  },
  {
    id: "org_activity",
    title: "Organization activity",
    description:
      "Day-to-day activity within your organization. On by default, turn it off if you would rather not follow along.",
    emailKey: "email_org_activity",
    inAppKey: "in_app_org_activity",
  },
];

function ChannelSwitch({
  enabled,
  disabled,
  ariaLabel,
  onClick,
}: {
  enabled: boolean;
  disabled: boolean;
  ariaLabel: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={enabled}
      aria-label={ariaLabel}
      disabled={disabled}
      onClick={onClick}
      className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30 disabled:cursor-not-allowed disabled:opacity-60 ${
        enabled ? "bg-success" : "bg-border"
      }`}
    >
      <span
        className={`inline-block h-5 w-5 transform rounded-full bg-white shadow transition ${
          enabled ? "translate-x-5" : "translate-x-0.5"
        }`}
      />
    </button>
  );
}

export default function NotificationsPage() {
  const [prefs, setPrefs] = useState<NotificationPreferences | null>(null);
  const [loadErr, setLoadErr] = useState("");
  const [saveErr, setSaveErr] = useState("");
  const [saveMsg, setSaveMsg] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoadErr("");
    apiFetch<NotificationPreferences>("/api/v1/notifications/preferences")
      .then((data) => {
        if (!cancelled) setPrefs(data);
      })
      .catch((err) => {
        if (!cancelled) setLoadErr(extractErrorMessage(err));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function toggle(key: PrefKey) {
    setSaveMsg("");
    setPrefs((current) =>
      current ? { ...current, [key]: !current[key] } : current,
    );
  }

  async function handleSave() {
    if (!prefs) return;
    setSaving(true);
    setSaveErr("");
    setSaveMsg("");
    try {
      // PUT replaces every toggle, so we send the whole eight-field shape.
      const updated = await apiFetch<NotificationPreferences>(
        "/api/v1/notifications/preferences",
        { method: "PUT", body: JSON.stringify(prefs) },
      );
      setPrefs(updated);
      setSaveMsg("Notification preferences saved.");
    } catch (err) {
      setSaveErr(extractErrorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <SettingsLayout activeTab="/settings/notifications">
      <div className="max-w-3xl space-y-6">
        <div className={`${card} p-6`}>
          <h2 className={`mb-2 ${cardTitle}`}>Notifications</h2>
          <p className="mb-5 text-sm text-text-muted">
            Choose how we reach you for each kind of update: by email, in the
            app&apos;s notification bell, or both. Security alerts are always on
            for both.
          </p>

          {loadErr && (
            <div role="alert" aria-live="polite" className={errorCls}>
              {loadErr}
            </div>
          )}

          {!loadErr && !prefs && (
            <div className="flex justify-center py-8" aria-live="polite">
              <Loader2
                className="h-6 w-6 animate-spin text-text-muted motion-reduce:animate-none"
                aria-label="Loading notification preferences"
              />
            </div>
          )}

          {prefs && (
            <div className="space-y-5">
              {saveMsg && (
                <div role="status" aria-live="polite" className={successCls}>
                  {saveMsg}
                </div>
              )}
              {saveErr && (
                <div role="alert" aria-live="polite" className={errorCls}>
                  {saveErr}
                </div>
              )}

              <div>
                <div className="flex items-center gap-4 border-b border-border pb-2 text-xs font-medium uppercase tracking-wide text-text-muted">
                  <div className="flex-1" />
                  <div className="w-14 text-center">Email</div>
                  <div className="w-14 text-center">In-app</div>
                </div>

                <ul className="divide-y divide-border">
                  {CATEGORIES.map((cat) => {
                    // Security in-app is hardcoded on (backend force-on) so a
                    // stale persisted false can never render as a lying OFF.
                    const emailEnabled = Boolean(prefs[cat.emailKey]);
                    const inAppEnabled = cat.locked
                      ? true
                      : Boolean(prefs[cat.inAppKey]);
                    return (
                      <li key={cat.id} className="flex items-start gap-4 py-4">
                        <div className="flex-1">
                          <p className="text-sm font-medium text-text-primary">
                            {cat.title}
                            {cat.locked && (
                              <span className="ml-2 text-xs font-normal text-text-muted">
                                (always on)
                              </span>
                            )}
                          </p>
                          <p className="mt-1 max-w-sm text-xs text-text-muted">
                            {cat.description}
                          </p>
                        </div>
                        <div className="flex w-14 justify-center pt-0.5">
                          <ChannelSwitch
                            enabled={emailEnabled}
                            disabled={cat.locked || saving}
                            ariaLabel={`${cat.title} email notifications`}
                            onClick={() => !cat.locked && toggle(cat.emailKey)}
                          />
                        </div>
                        <div className="flex w-14 justify-center pt-0.5">
                          <ChannelSwitch
                            enabled={inAppEnabled}
                            disabled={cat.locked || saving}
                            ariaLabel={`${cat.title} in-app notifications`}
                            onClick={() => !cat.locked && toggle(cat.inAppKey)}
                          />
                        </div>
                      </li>
                    );
                  })}
                </ul>
              </div>

              <button
                type="button"
                onClick={handleSave}
                disabled={saving}
                aria-busy={saving}
                className={`${btnPrimary} w-full sm:w-auto sm:min-h-0 inline-flex items-center justify-center gap-2`}
              >
                {saving && (
                  <Loader2
                    className="h-4 w-4 animate-spin motion-reduce:animate-none"
                    aria-hidden="true"
                  />
                )}
                {saving ? "Saving..." : "Save changes"}
              </button>
            </div>
          )}
        </div>
      </div>
    </SettingsLayout>
  );
}
