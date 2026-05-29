// Faq — eight common questions. Uses native <details>/<summary> for
// disclosure semantics. Native elements give us:
//   * Keyboard support (Enter / Space to toggle) for free.
//   * Correct ARIA semantics without aria-expanded plumbing.
//   * Server-renderable, no client JS, no React state.
//
// No em-dashes in any copy (locked policy `feedback_no_em_dashes`).
// Answers are short, honest, and grounded in current product reality.

const items = [
  {
    q: "Is my data secure?",
    a: "Yes. All data lives in an EU data center, encrypted at rest. Account credentials are stored as bcrypt hashes. We use HTTPS everywhere and never store your bank login credentials.",
  },
  {
    q: "Can I export my data?",
    a: "Yes. Every list view exports to CSV, and a one-click full org export is in the works. Your data is always yours.",
  },
  {
    q: "Do you use my data to train AI?",
    a: "No. Personal financial data is never used to train models. The optional AI assistant runs against a provider you choose, and you can disable it at any time.",
  },
  {
    q: "Can I delete my account?",
    a: "Yes. Account deletion is one click in Settings. It hard-deletes your data within seven days, and you receive a confirmation email when the deletion completes.",
  },
  {
    q: "Do I need to connect my bank?",
    a: "No. You can import a CSV from your bank, or add transactions manually. Direct bank connections are on the roadmap but not required to get the full value out of the app.",
  },
  {
    q: "Is it built for one person or a couple?",
    a: "Both. The data model is org-scoped, so you start as a one-person org and can invite a partner or housemate later. Each org has its own categories, accounts, and reports.",
  },
];

export default function Faq() {
  return (
    <section
      id="faq"
      aria-labelledby="faq-heading"
      className="mx-auto max-w-3xl px-6 py-20 lg:py-24"
    >
      <div className="mb-10">
        <p className="mb-3 font-display text-xs font-semibold uppercase tracking-[0.18em] text-text-muted">
          Questions you might have
        </p>
        <h2
          id="faq-heading"
          className="font-display text-3xl font-semibold leading-tight text-text-primary lg:text-4xl"
        >
          Frequently asked.
        </h2>
      </div>
      <ul className="space-y-3">
        {items.map((item) => (
          <li
            key={item.q}
            className="rounded-xl border border-border bg-surface motion-safe:animate-fade-in-up"
          >
            {/* `group` lets the chevron rotate on open via the `open:`
                pseudo-class (native <details[open]> state). No JS. */}
            <details className="group">
              <summary
                className="flex cursor-pointer list-none items-center justify-between gap-4 rounded-xl px-5 py-4 text-left text-sm font-medium text-text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 lg:text-base"
              >
                <span>{item.q}</span>
                <ChevronGlyph />
              </summary>
              <div className="border-t border-border px-5 py-4 text-sm leading-relaxed text-text-secondary">
                {item.a}
              </div>
            </details>
          </li>
        ))}
      </ul>
    </section>
  );
}

function ChevronGlyph() {
  return (
    <svg
      viewBox="0 0 16 16"
      aria-hidden
      className="h-4 w-4 flex-shrink-0 text-text-muted transition-transform group-open:rotate-180"
    >
      <path
        d="M3 6l5 5 5-5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
