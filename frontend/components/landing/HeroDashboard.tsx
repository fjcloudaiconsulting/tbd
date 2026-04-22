export default function HeroDashboard() {
  return (
    <div
      aria-hidden
      className="rounded-xl border border-border bg-surface-raised p-6 shadow-2xl"
    >
      <div className="mb-1 text-[10px] font-medium uppercase tracking-[0.12em] text-text-muted">
        April balance
      </div>
      <div className="mb-5 font-display text-3xl font-semibold text-text-primary">
        €4,283.12
      </div>

      <div className="mb-1 flex items-end gap-1.5">
        {[70, 55, 80, 45, 30, 65, 40, 75, 50, 60, 35, 70].map((h, i) => (
          <div
            key={i}
            className={`flex-1 rounded-sm ${i === 4 || i === 6 || i === 10 ? "bg-danger/80" : "bg-success/80"}`}
            style={{ height: `${h}px` }}
          />
        ))}
      </div>
      <div className="mb-5 text-[10px] text-text-muted">
        Weekly spend · forecast overlay
      </div>

      <div className="space-y-3">
        <BudgetRow label="Groceries" spent="€412" limit="€500" percent={82} over={false} />
        <BudgetRow label="Dining" spent="€189" limit="€150" percent={100} over />
        <BudgetRow label="Transport" spent="€71" limit="€120" percent={59} over={false} />
      </div>
    </div>
  );
}

function BudgetRow({
  label,
  spent,
  limit,
  percent,
  over,
}: {
  label: string;
  spent: string;
  limit: string;
  percent: number;
  over: boolean;
}) {
  return (
    <div>
      <div className="mb-1 flex justify-between text-xs text-text-secondary">
        <span>{label}</span>
        <span>
          {spent} / {limit}
        </span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-border">
        <div
          className={`h-full ${over ? "bg-danger" : "bg-success"}`}
          style={{ width: `${Math.min(percent, 100)}%` }}
        />
      </div>
    </div>
  );
}
