import React from "react";
import { card, cardTitle } from "@/lib/styles";

interface StatCardProps {
  label: string;
  value: React.ReactNode;
  valueClassName?: string;
  valueSize?: string;
  sub?: React.ReactNode;
  subClassName?: string;
  badge?: React.ReactNode;
}

export default function StatCard({ label, value, valueClassName, valueSize, sub, subClassName, badge }: StatCardProps) {
  const resolvedValueSize = valueSize ?? "text-2xl";
  const resolvedSubClassName = subClassName ?? "mt-1 text-sm text-text-muted";
  return (
    <div className={`${card} p-5`}>
      <p className={cardTitle}>{label}</p>
      {badge ? (
        <div className="mt-1 flex flex-wrap items-center gap-2">
          <p className={`${resolvedValueSize} font-semibold tabular-nums ${valueClassName ?? "text-text-primary"}`}>{value}</p>
          {badge}
        </div>
      ) : (
        <p className={`mt-1 ${resolvedValueSize} font-semibold tabular-nums ${valueClassName ?? "text-text-primary"}`}>{value}</p>
      )}
      {sub ? <p data-testid="stat-card-sub" className={resolvedSubClassName}>{sub}</p> : null}
    </div>
  );
}
