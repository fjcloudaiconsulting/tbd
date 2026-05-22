// ScreenshotShowcase — three in-code product previews (Transactions,
// Reports, Plans). We deliberately render mock UI here rather than
// loading PNG screenshots:
//
//   1. The product surface changes weekly during pre-launch; static
//      screenshots would go stale fast and ship a wrong-looking app.
//   2. PNGs would bypass theme tokens — the previews would not respect
//      light/dark theme switches, breaking the rest of the landing.
//   3. The brand voice already includes HeroDashboard as an in-code
//      preview; this section extends that pattern to three more
//      surfaces with the same fidelity.
//
// When we DO have polished marketing screenshots (post-launch), the
// path convention is `public/screenshots/<surface>.png` and these
// frames can swap to <Image /> tags. The placeholder strategy is
// documented in the L5.1 PR body so designers know where to drop
// real shots.
//
// Layout: stacked alternating left/right on desktop, vertical stack
// on mobile. Each frame carries a short caption that ties the
// preview to a real product capability.

const previews = [
  {
    surface: "Transactions",
    caption: "Categorize as you go. Auto-suggestions learn from your edits.",
    body: <TransactionsPreview />,
  },
  {
    surface: "Reports",
    caption: "Custom canvas of cards. Add KPIs, charts, and filter strips.",
    body: <ReportsPreview />,
  },
  {
    surface: "Plans",
    caption: "Run a what-if for retirement, a house, or a long trip.",
    body: <PlansPreview />,
  },
];

export default function ScreenshotShowcase() {
  return (
    <section
      id="product"
      aria-label="Product previews"
      className="mx-auto max-w-6xl px-6 py-20 lg:px-10 lg:py-24"
    >
      <div className="mb-12 max-w-2xl">
        <p className="mb-3 font-display text-xs font-semibold uppercase tracking-[0.18em] text-text-muted">
          See it in motion
        </p>
        <h2 className="font-display text-3xl font-semibold leading-tight text-text-primary lg:text-4xl">
          Built for the way you actually look at money.
        </h2>
        <p className="mt-4 text-base leading-relaxed text-text-secondary">
          Every view answers a single question. No dashboards stuffed
          with widgets you never read.
        </p>
      </div>
      <div className="space-y-16 lg:space-y-24">
        {previews.map((p, i) => (
          <div
            key={p.surface}
            className={`grid items-center gap-8 lg:grid-cols-2 lg:gap-16 ${
              i % 2 === 1 ? "lg:[&>div:first-child]:order-2" : ""
            }`}
          >
            <div className="motion-safe:animate-fade-in-up">
              <p className="mb-3 font-display text-xs font-semibold uppercase tracking-[0.14em] text-accent">
                {p.surface}
              </p>
              <p className="text-lg leading-relaxed text-text-primary lg:text-xl">
                {p.caption}
              </p>
            </div>
            <div className="motion-safe:animate-fade-in">
              {/* Each preview is wrapped in a chrome frame that echoes
                  the product's app shell, so visitors see the previews
                  AS the product, not as decorative art. The frame is
                  decorative for assistive tech — the caption to the
                  left carries the meaning. */}
              <div
                role="img"
                aria-label={`${p.surface} preview`}
                className="overflow-hidden rounded-xl border border-border bg-surface-raised shadow-card"
              >
                <div className="flex items-center gap-1.5 border-b border-border bg-surface px-3 py-2">
                  <span className="h-2.5 w-2.5 rounded-full bg-border" aria-hidden />
                  <span className="h-2.5 w-2.5 rounded-full bg-border" aria-hidden />
                  <span className="h-2.5 w-2.5 rounded-full bg-border" aria-hidden />
                  <span className="ml-3 text-[10px] uppercase tracking-[0.12em] text-text-muted">
                    {p.surface.toLowerCase()}.thebetterdecision.com
                  </span>
                </div>
                <div className="p-5">{p.body}</div>
              </div>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function TransactionsPreview() {
  const rows = [
    { date: "Apr 14", desc: "Albert Heijn", cat: "Groceries", amt: "-€42.18", tone: "danger" as const },
    { date: "Apr 14", desc: "Salary, ACME", cat: "Income", amt: "+€3,420.00", tone: "success" as const },
    { date: "Apr 13", desc: "NS Spoor", cat: "Transport", amt: "-€8.40", tone: "danger" as const },
    { date: "Apr 12", desc: "Cafe Lumière", cat: "Dining", amt: "-€14.50", tone: "danger" as const },
    { date: "Apr 12", desc: "Bol.com refund", cat: "Shopping", amt: "+€29.99", tone: "success" as const },
  ];
  return (
    <div aria-hidden>
      <div className="mb-3 grid grid-cols-[1fr_2fr_1.2fr_auto] gap-3 border-b border-border pb-2 text-[10px] font-medium uppercase tracking-[0.08em] text-text-muted">
        <div>Date</div>
        <div>Description</div>
        <div>Category</div>
        <div className="text-right">Amount</div>
      </div>
      <div className="space-y-2">
        {rows.map((r) => (
          <div
            key={r.desc}
            className="grid grid-cols-[1fr_2fr_1.2fr_auto] items-center gap-3 text-xs text-text-secondary"
          >
            <div className="tabular-nums">{r.date}</div>
            <div className="text-text-primary">{r.desc}</div>
            <div>
              <span className="inline-flex items-center rounded-full border border-border bg-surface px-2 py-0.5 text-[10px] text-text-secondary">
                {r.cat}
              </span>
            </div>
            <div
              className={`text-right font-medium tabular-nums ${
                r.tone === "success" ? "text-success" : "text-text-primary"
              }`}
            >
              {r.amt}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function ReportsPreview() {
  return (
    <div aria-hidden className="space-y-4">
      <div className="grid grid-cols-3 gap-3">
        {[
          { label: "Net worth", value: "€48,210" },
          { label: "This month", value: "+€1,840" },
          { label: "Avg. monthly", value: "€2,103" },
        ].map((k) => (
          <div key={k.label} className="rounded-lg border border-border bg-surface p-3">
            <div className="mb-1 text-[10px] font-medium uppercase tracking-[0.08em] text-text-muted">
              {k.label}
            </div>
            <div className="text-base font-semibold tabular-nums text-text-primary">
              {k.value}
            </div>
          </div>
        ))}
      </div>
      <div className="rounded-lg border border-border bg-surface p-3">
        <div className="mb-2 flex items-center justify-between text-[10px] uppercase tracking-[0.08em] text-text-muted">
          <span>Spend by category</span>
          <span>last 6 months</span>
        </div>
        <div className="flex items-end gap-1.5">
          {[40, 55, 35, 80, 65, 50, 75, 60, 45, 70, 90, 55].map((h, i) => (
            <div
              key={i}
              className="flex-1 rounded-sm bg-accent/60"
              style={{ height: `${h * 0.6}px` }}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

function PlansPreview() {
  return (
    <div aria-hidden>
      <div className="mb-3 flex items-center justify-between">
        <div>
          <div className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-muted">
            Scenario
          </div>
          <div className="text-sm font-semibold text-text-primary">Retire at 60</div>
        </div>
        <span className="rounded-full bg-success-dim px-2 py-0.5 text-[10px] font-medium text-success">
          On track
        </span>
      </div>
      <div className="mb-4 rounded-lg border border-border bg-surface p-3">
        <div className="mb-2 text-[10px] uppercase tracking-[0.08em] text-text-muted">
          Projected balance
        </div>
        <svg viewBox="0 0 200 60" className="h-16 w-full" aria-hidden>
          <path
            d="M0,55 L20,48 L40,42 L60,35 L80,30 L100,22 L120,18 L140,12 L160,8 L180,5 L200,2"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            className="text-accent"
          />
          <path
            d="M0,55 L20,48 L40,42 L60,35 L80,30 L100,22 L120,18 L140,12 L160,8 L180,5 L200,2 L200,60 L0,60 Z"
            className="fill-accent/15"
          />
        </svg>
      </div>
      <div className="space-y-2 text-xs">
        <div className="flex justify-between">
          <span className="text-text-secondary">Monthly contribution</span>
          <span className="tabular-nums text-text-primary">€620</span>
        </div>
        <div className="flex justify-between">
          <span className="text-text-secondary">Expected return</span>
          <span className="tabular-nums text-text-primary">4.5% / yr</span>
        </div>
        <div className="flex justify-between border-t border-border pt-2">
          <span className="text-text-secondary">Balance at 60</span>
          <span className="tabular-nums font-semibold text-text-primary">€482,300</span>
        </div>
      </div>
    </div>
  );
}
