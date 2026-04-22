const tiles = [
  {
    title: "See your money clearly",
    sub: "All your accounts and transactions, categorized, in one dashboard.",
  },
  {
    title: "Plan what's coming",
    sub: "Budgets, forecasts, and recurring transactions — so surprises stay rare.",
  },
  {
    title: "Shared, if you want",
    sub: "Built for households: one org, multiple people, clear boundaries.",
  },
  {
    title: "Your data stays yours",
    sub: "EU-hosted today. Never sold, never shared, never moved across borders without asking. More regions coming.",
  },
];

export default function FeatureTiles() {
  return (
    <section className="mx-auto max-w-6xl px-6 py-16 lg:px-10 lg:py-24">
      <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-4 lg:gap-8">
        {tiles.map((tile, i) => (
          <div
            key={tile.title}
            className="rounded-xl border border-border bg-surface p-6"
          >
            <div className="mb-3 font-display text-xs font-semibold uppercase tracking-[0.14em] text-text-muted">
              {String(i + 1).padStart(2, "0")}
            </div>
            <h3 className="mb-2 font-display text-lg font-semibold leading-snug text-text-primary">
              {tile.title}
            </h3>
            <p className="text-sm leading-relaxed text-text-secondary">
              {tile.sub}
            </p>
          </div>
        ))}
      </div>
    </section>
  );
}
