"use client";

import { useEffect, useRef, useState } from "react";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import {
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
} from "@/lib/styles";

type FeatureName = "reports" | "plans" | "custom_dashboard";
type TriState = "on" | "off" | "inherit";

interface OrgFeatureGate {
  feature: FeatureName;
  override: TriState;
  effective: boolean;
}

const FEATURE_LABELS: Record<FeatureName, string> = {
  reports: "Reports",
  plans: "Plans",
  custom_dashboard: "Customizable dashboard",
};

interface Props {
  orgId: number;
}

export default function OrgFeatureGateCard({ orgId }: Props) {
  const [gates, setGates] = useState<OrgFeatureGate[]>([]);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState("");
  // Per-feature save status: undefined = idle, "saving" = in-flight, "ok" = success, string = error
  const [saveStatus, setSaveStatus] = useState<Record<string, "saving" | "ok" | string>>({});
  const clearTimers = useRef<number[]>([]);

  useEffect(() => {
    return () => {
      clearTimers.current.forEach(clearTimeout);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setFetchError("");
      try {
        const data = await apiFetch<OrgFeatureGate[]>(`/api/v1/admin/orgs/${orgId}/features`);
        if (!cancelled) setGates(data);
      } catch (err) {
        if (!cancelled) setFetchError(extractErrorMessage(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void load();
    return () => { cancelled = true; };
  }, [orgId]);

  async function handleChange(feature: FeatureName, value: TriState) {
    setSaveStatus((s) => ({ ...s, [feature]: "saving" }));
    try {
      const updated = await apiFetch<OrgFeatureGate>(
        `/api/v1/admin/orgs/${orgId}/features/${feature}`,
        { method: "PUT", body: JSON.stringify({ value }) },
      );
      setGates((prev) =>
        prev.map((g) => (g.feature === feature ? updated : g)),
      );
      setSaveStatus((s) => ({ ...s, [feature]: "ok" }));
      const timerId = window.setTimeout(
        () => setSaveStatus((s) => ({ ...s, [feature]: "" })),
        3000,
      );
      clearTimers.current.push(timerId);
    } catch (err) {
      setSaveStatus((s) => ({ ...s, [feature]: extractErrorMessage(err) }));
    }
  }

  return (
    <div className={`${card} mb-6`}>
      <div className={cardHeader}>
        <h2 className={cardTitle}>Feature Access (Reports &amp; Plans)</h2>
        <p className="mt-1 text-xs text-text-muted">
          Globally-gated features. Override the system default for this organization.
        </p>
      </div>

      <div className="p-6">
        {fetchError && (
          <p className={`${errorCls} mb-4`}>{fetchError}</p>
        )}
        {loading && (
          <p className="text-sm text-text-muted">Loading feature gates…</p>
        )}
        {!loading && !fetchError && gates.length === 0 && (
          <p className="text-sm text-text-muted">No features found.</p>
        )}

        {!loading && gates.length > 0 && (
          <div className="flex flex-col divide-y divide-border">
            {gates.map((gate) => {
              const status = saveStatus[gate.feature];

              return (
                <div
                  key={gate.feature}
                  className="flex flex-col gap-2 py-4 first:pt-0 last:pb-0 sm:flex-row sm:items-center sm:justify-between"
                >
                  <div className="flex flex-col gap-0.5">
                    <span className="text-sm font-medium text-text-primary">
                      {FEATURE_LABELS[gate.feature] ?? gate.feature}
                    </span>
                    <span className="text-xs text-text-muted">
                      Effective:{" "}
                      <span className={`font-medium ${gate.effective ? "text-success" : "text-text-secondary"}`}>
                        {gate.effective ? "Enabled" : "Disabled"}
                      </span>
                    </span>
                  </div>

                  <div className="flex flex-col items-start gap-1.5 sm:items-end">
                    <div
                      role="group"
                      aria-label={`${FEATURE_LABELS[gate.feature] ?? gate.feature} per-org override`}
                      className="inline-flex rounded-md border border-border bg-surface-raised overflow-hidden"
                    >
                      {(["on", "off", "inherit"] as TriState[]).map((opt) => (
                        <button
                          key={opt}
                          type="button"
                          disabled={status === "saving"}
                          aria-pressed={gate.override === opt}
                          onClick={() => {
                            if (gate.override !== opt) void handleChange(gate.feature, opt);
                          }}
                          className={[
                            "px-3 py-1.5 text-xs font-medium capitalize transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/30",
                            gate.override === opt
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
