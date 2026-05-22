// Testimonials — three placeholder cards. The names, roles, and quotes
// below are HOLD-OVER copy modeled on the kinds of feedback we expect
// from the closed beta. They are intentionally generic, brand-voice
// consistent, and free of any unverifiable specifics (no real
// employer names, no metrics).
//
// TODO when we have customer permissions on file:
//   1. Replace each `{ initials, name, role, quote, location }` row
//      with real testimonial copy.
//   2. Confirm in writing that the quoted person has agreed to be
//      named on the public marketing site.
//   3. If the customer prefers anonymity, keep initials only and drop
//      the surname — never invent a name.
//
// Layout: three-card row, single column on mobile. Subtle hover lift
// (motion-safe).

const items = [
  {
    initials: "AS",
    name: "Anna S.",
    role: "Designer, Amsterdam",
    quote:
      "I tried four budgeting apps before this. The Better Decision is the first one I open without flinching. Forecasts are a quiet superpower.",
  },
  {
    initials: "MR",
    name: "Marco R.",
    role: "Engineer, Lisbon",
    quote:
      "Categories that learn from edits, recurring bills that just work, and an actual forecast. I closed three spreadsheets the week I switched.",
  },
  {
    initials: "JK",
    name: "Jana K.",
    role: "Freelance writer, Berlin",
    quote:
      "Money used to give me anxiety on Sunday nights. Now I have a calm view of the month and a plan for the next one. That is the product.",
  },
];

export default function Testimonials() {
  return (
    <section
      aria-labelledby="testimonials-heading"
      className="mx-auto max-w-6xl px-6 py-20 lg:px-10 lg:py-24"
    >
      <div className="mb-10 max-w-2xl">
        <p className="mb-3 font-display text-xs font-semibold uppercase tracking-[0.18em] text-text-muted">
          From the early users
        </p>
        <h2
          id="testimonials-heading"
          className="font-display text-3xl font-semibold leading-tight text-text-primary lg:text-4xl"
        >
          Calmer money decisions, in their words.
        </h2>
      </div>
      <ul className="grid gap-6 md:grid-cols-3 lg:gap-8">
        {items.map((t) => (
          <li
            key={t.name}
            className="flex flex-col rounded-xl border border-border bg-surface p-6 transition-transform motion-safe:hover:-translate-y-0.5 motion-safe:animate-fade-in-up"
          >
            <QuoteGlyph />
            <p className="mt-4 flex-1 text-sm leading-relaxed text-text-primary">
              {t.quote}
            </p>
            <footer className="mt-6 flex items-center gap-3 border-t border-border pt-4">
              <span
                aria-hidden
                className="inline-flex h-10 w-10 items-center justify-center rounded-full bg-accent-dim font-display text-sm font-semibold text-accent"
              >
                {t.initials}
              </span>
              <div>
                <div className="text-sm font-medium text-text-primary">
                  {t.name}
                </div>
                <div className="text-xs text-text-muted">{t.role}</div>
              </div>
            </footer>
          </li>
        ))}
      </ul>
    </section>
  );
}

function QuoteGlyph() {
  return (
    <svg
      viewBox="0 0 24 24"
      aria-hidden
      className="h-6 w-6 text-accent/70"
    >
      <path
        d="M9 7H5a2 2 0 00-2 2v4a2 2 0 002 2h2v2a3 3 0 01-3 3v2a5 5 0 005-5V9a2 2 0 00-2-2zm10 0h-4a2 2 0 00-2 2v4a2 2 0 002 2h2v2a3 3 0 01-3 3v2a5 5 0 005-5V9a2 2 0 00-2-2z"
        fill="currentColor"
      />
    </svg>
  );
}
