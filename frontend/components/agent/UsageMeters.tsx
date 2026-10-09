"use client";

// This period's use of each plan meter, from GET /api/v1/ai/status (TBD-581).
// The text carries the meaning; the bar only repeats it.

import { METER_LABELS } from "@/lib/agent/present";
import { formatMoney } from "@/lib/format";
import type { MeterUsage } from "@/lib/types";
import { badgeWarning, card, cardHeader, cardTitle } from "@/lib/styles";

const count = (n: number) => n.toLocaleString();

// Platform AI spend is metered in USD cents whatever the org's currency.
function amount(meter: string, n: number): string {
  return meter === "platform_ai.cents" ? formatMoney(n / 100, "USD") : count(n);
}

// Boundaries are UTC midnights; showing them in local time would read as the
// day before for anyone west of UTC.
function resetDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", timeZone: "UTC" });
}

export function meterLine(meter: string, u: MeterUsage): string {
  if (u.limit === 0) return "Not in your plan";
  const per = u.period === "day" ? "today" : "this month";
  if (u.limit === null) return `${amount(meter, u.used)} used ${per}, no limit`;
  const resets = u.resets_at ? `, resets ${resetDate(u.resets_at)}` : "";
  return `${amount(meter, u.used)} of ${amount(meter, u.limit)} ${per}${resets}`;
}

export default function UsageMeters({
  usage, meters, title = "Usage",
}: {
  usage: Record<string, MeterUsage>;
  meters?: string[];
  title?: string;
}) {
  const keys = (meters ?? Object.keys(usage)).filter((m) => usage[m]);
  if (keys.length === 0) return null;
  return (
    <section className={`${card} mb-6`} aria-labelledby="usage-title" data-testid="usage-meters">
      <div className={cardHeader}>
        <h2 id="usage-title" className={cardTitle}>{title}</h2>
      </div>
      <ul className="divide-y divide-border-subtle">
        {keys.map((m) => {
          const u = usage[m];
          const share = u.limit ? Math.min(1, u.used / u.limit) : 0;
          const full = u.limit !== null && u.limit > 0 && u.used >= u.limit;
          const fill = full ? "bg-danger" : share >= 0.8 ? "bg-warning" : "bg-border-strong";
          return (
            <li key={m} className="px-6 py-3">
              <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
                <span className="text-sm text-text-primary">{METER_LABELS[m] ?? m}</span>
                <span className="flex items-center gap-2 text-xs text-text-secondary tabular-nums">
                  {full && <span className={badgeWarning}>Limit reached</span>}
                  {meterLine(m, u)}
                </span>
              </div>
              {u.limit !== null && u.limit > 0 && (
                <div aria-hidden className="mt-2 h-1.5 overflow-hidden rounded-full bg-surface-raised">
                  <div className={`h-full rounded-full ${fill}`} style={{ width: `${share * 100}%` }} />
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
