"use client";

import { useEffect, useState } from "react";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import {
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
  success as successCls,
} from "@/lib/styles";

type FeatureName = "reports" | "plans";
type TriState = "on" | "off" | "inherit";

interface FeatureFlag {
  feature: FeatureName;
  global_value: "on" | "off" | null;
  env_floor: boolean;
}

const FEATURE_LABELS: Record<FeatureName, string> = {
  reports: "Reports",
  plans: "Plans",
};

function toTriState(global_value: "on" | "off" | null): TriState {
  if (global_value === "on") return "on";
  if (global_value === "off") return "off";
  return "inherit";
}

export default function FeatureFlagsCard() {
  const [flags, setFlags] = useState<FeatureFlag[]>([]);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState("");
  // Per-feature save status: undefined = idle, "saving" = in-flight, "ok" = success, string = error
  const [saveStatus, setSaveStatus] = useState<Record<string, "saving" | "ok" | string>>({});

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setFetchError("");
      try {
        const data = await apiFetch<FeatureFlag[]>("/api/v1/admin/features");
        if (!cancelled) setFlags(data);
      } catch (err) {
        if (!cancelled) setFetchError(extractErrorMessage(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void load();
    return () => { cancelled = true; };
  }, []);

  async function handleChange(feature: FeatureName, value: TriState) {
    setSaveStatus((s) => ({ ...s, [feature]: "saving" }));
    try {
      const updated = await apiFetch<FeatureFlag>(
        `/api/v1/admin/features/${feature}`,
        { method: "PUT", body: JSON.stringify({ value }) },
      );
      setFlags((prev) =>
        prev.map((f) => (f.feature === feature ? updated : f)),
      );
      setSaveStatus((s) => ({ ...s, [feature]: "ok" }));
      setTimeout(() => setSaveStatus((s) => ({ ...s, [feature]: "" })), 3000);
    } catch (err) {
      setSaveStatus((s) => ({ ...s, [feature]: extractErrorMessage(err) }));
    }
  }

  return (
    <div className={card}>
      <div className={cardHeader}>
        <h2 className={cardTitle}>Global Feature Flags</h2>
      </div>

      <div className="p-6">
        {fetchError && (
          <p className={`${errorCls} mb-4`}>{fetchError}</p>
        )}
        {loading && (
          <p className="text-sm text-text-muted">Loading feature flags…</p>
        )}
        {!loading && !fetchError && flags.length === 0 && (
          <p className="text-sm text-text-muted">No features found.</p>
        )}

        {!loading && flags.length > 0 && (
          <div className="flex flex-col divide-y divide-border">
            {flags.map((flag) => {
              const current = toTriState(flag.global_value);
              const status = saveStatus[flag.feature];
              const envLabel = flag.env_floor ? "on" : "off";

              return (
                <div key={flag.feature} className="flex flex-col gap-2 py-4 first:pt-0 last:pb-0 sm:flex-row sm:items-center sm:justify-between">
                  <div className="flex flex-col gap-0.5">
                    <span className="text-sm font-medium text-text-primary">
                      {FEATURE_LABELS[flag.feature] ?? flag.feature}
                    </span>
                    {current === "inherit" && (
                      <span className="text-xs text-text-muted">
                        Inheriting environment default: <span className="font-medium text-text-secondary">{envLabel}</span>
                      </span>
                    )}
                  </div>

                  <div className="flex flex-col items-start gap-1.5 sm:items-end">
                    <div
                      role="group"
                      aria-label={`${FEATURE_LABELS[flag.feature] ?? flag.feature} global value`}
                      className="inline-flex rounded-md border border-border bg-surface-raised overflow-hidden"
                    >
                      {(["on", "off", "inherit"] as TriState[]).map((opt) => (
                        <button
                          key={opt}
                          type="button"
                          disabled={status === "saving"}
                          aria-pressed={current === opt}
                          onClick={() => {
                            if (current !== opt) void handleChange(flag.feature, opt);
                          }}
                          className={[
                            "px-3 py-1.5 text-xs font-medium capitalize transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/30",
                            current === opt
                              ? "bg-accent text-accent-text"
                              : "text-text-secondary hover:bg-surface hover:text-text-primary",
                          ].join(" ")}
                        >
                          {opt}
                        </button>
                      ))}
                    </div>

                    {status === "ok" && (
                      <p className="text-xs text-success">Saved</p>
                    )}
                    {status && status !== "saving" && status !== "ok" && (
                      <p className="text-xs text-danger">{status}</p>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
