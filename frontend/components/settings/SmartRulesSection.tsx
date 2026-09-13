"use client";

import { useEffect, useState } from "react";

import Switch from "@/components/ui/Switch";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import {
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
} from "@/lib/styles";
import type { OrgSetting } from "@/lib/types";

const SETTING_KEY = "share_merchant_data";

export default function SmartRulesSection() {
  const [enabled, setEnabled] = useState<boolean>(false);
  const [loading, setLoading] = useState<boolean>(true);
  const [saving, setSaving] = useState<boolean>(false);
  const [error, setError] = useState<string>("");
  // TBD-323: a switch states its value in text, so it must never render a
  // value that was not read. No switch while loading, none after a failed load.
  const [loadError, setLoadError] = useState<string>("");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const settings = await apiFetch<OrgSetting[]>("/api/v1/settings");
        if (cancelled) return;
        const row = settings.find((s) => s.key === SETTING_KEY);
        setEnabled(row?.value === "true");
      } catch (err) {
        if (!cancelled) {
          setLoadError(extractErrorMessage(err, "Could not load this preference."));
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function toggle() {
    if (saving || loading) return;
    const next = !enabled;
    setSaving(true);
    setError("");
    try {
      await apiFetch("/api/v1/settings", {
        method: "PUT",
        body: JSON.stringify({ key: SETTING_KEY, value: next ? "true" : "false" }),
      });
      setEnabled(next);
    } catch (err) {
      setError(extractErrorMessage(err, "Could not update preference"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className={card}>
      <header className={cardHeader}>
        <h2 className={cardTitle}>Smart rules</h2>
      </header>
      <div className="p-6">
        {error && (
          <div className={`${errorCls} mb-4`} role="alert">
            {error}
          </div>
        )}
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="text-sm text-text-primary">
              Share anonymized merchant data
            </p>
            <p className="mt-1 max-w-md text-xs text-text-muted">
              Help improve auto-categorization for everyone (anonymized, only
              merchant tokens, never your transaction details).
            </p>
          </div>
          {loading ? (
            <p className="text-sm text-text-muted">Loading...</p>
          ) : loadError ? (
            <p role="alert" className="text-sm text-danger">
              {loadError}
            </p>
          ) : (
            <Switch
              checked={enabled}
              onChange={() => void toggle()}
              label="Share anonymized merchant data"
              pending={saving}
            />
          )}
        </div>
      </div>
    </section>
  );
}
