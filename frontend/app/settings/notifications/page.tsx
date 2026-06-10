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

/**
 * The four notification categories, in display order. `security` is
 * listed first and is locked on: the backend rejects
 * `email_security=false` with code=security_emails_required, so the
 * toggle renders disabled+on with explanatory copy rather than letting
 * the user attempt a change the API will refuse.
 *
 * `key` is the email-channel field on NotificationPreferences. The
 * in-app channel fields are preserved verbatim on save (the PUT
 * replaces every toggle), so this page only ever mutates the email
 * side.
 */
const CATEGORIES: ReadonlyArray<{
  key: keyof Pick<
    NotificationPreferences,
    "email_security" | "email_account" | "email_org_admin" | "email_org_activity"
  >;
  title: string;
  description: string;
  locked?: boolean;
}> = [
  {
    key: "email_security",
    title: "Security",
    description:
      "Sign-in alerts, password changes, and other account-protection events. Always on so you never miss a security warning.",
    locked: true,
  },
  {
    key: "email_account",
    title: "Account",
    description:
      "Updates about your own account, like email verification and profile changes.",
  },
  {
    key: "email_org_admin",
    title: "Organization (admin)",
    description:
      "Administrative events for your organization, such as member invites and role changes.",
  },
  {
    key: "email_org_activity",
    title: "Organization activity",
    description:
      "Day-to-day activity within your organization. Quiet by default, turn it on to follow along.",
  },
];

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

  function toggle(key: (typeof CATEGORIES)[number]["key"]) {
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
      // PUT replaces every toggle, so we send the whole shape. The
      // in-app fields ride along untouched; this page only edits email.
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
      <div className="max-w-lg space-y-6">
        <div className={`${card} p-6`}>
          <h2 className={`mb-2 ${cardTitle}`}>Email notifications</h2>
          <p className="mb-5 text-sm text-text-muted">
            Choose which emails we send you. These settings only affect
            email, your in-app notifications are unchanged.
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

              <ul className="divide-y divide-border">
                {CATEGORIES.map((cat) => {
                  const enabled = prefs[cat.key];
                  return (
                    <li
                      key={cat.key}
                      className="flex items-start justify-between gap-4 py-4 first:pt-0 last:pb-0"
                    >
                      <div>
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
                      <button
                        type="button"
                        role="switch"
                        aria-checked={enabled}
                        aria-label={`${cat.title} email notifications`}
                        disabled={cat.locked || saving}
                        onClick={() => !cat.locked && toggle(cat.key)}
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
                    </li>
                  );
                })}
              </ul>

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
