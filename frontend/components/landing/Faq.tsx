// Faq — eight common questions. Uses native <details>/<summary> for
// disclosure semantics. Native elements give us:
//   * Keyboard support (Enter / Space to toggle) for free.
//   * Correct ARIA semantics without aria-expanded plumbing.
//   * Server-renderable, no client JS, no React state.
//
// No em-dashes in any copy (locked policy `feedback_no_em_dashes`).
// Answers are short, honest, and grounded in current product reality.
//
// FAQ entry data lives in faqData.ts so the landing page's JSON-LD
// structured data block can share the same source without drifting.

import { faqEntries } from "./faqData";
import ChevronGlyph from "./ChevronGlyph";

export default function Faq() {
  return (
    <section
      id="faq"
      aria-labelledby="faq-heading"
      className="mx-auto max-w-3xl px-6 py-20 lg:py-24"
    >
      <div className="mb-10">
        <h2
          id="faq-heading"
          className="font-display text-3xl font-semibold leading-tight text-text-primary lg:text-4xl"
        >
          Frequently asked.
        </h2>
      </div>
      <ul className="space-y-3">
        {faqEntries.map((item) => (
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
