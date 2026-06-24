import React from "react";
import { card, cardTitle } from "@/lib/styles";

interface StatCardProps {
  label: string;
  value: React.ReactNode;
  valueClassName?: string;
  sub?: React.ReactNode;
  badge?: React.ReactNode;
}

export default function StatCard({ label, value, valueClassName, sub, badge }: StatCardProps) {
  return (
    <div className={`${card} p-5`}>
      <p className={cardTitle}>{label}</p>
      <div className="mt-1 flex flex-wrap items-center gap-2">
        <p className={`text-2xl font-semibold tabular-nums ${valueClassName ?? "text-text-primary"}`}>{value}</p>
        {badge}
      </div>
      {sub ? <p className="mt-1 text-sm text-text-muted">{sub}</p> : null}
    </div>
  );
}
