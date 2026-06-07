// "How it works" — a three-step concrete journey between FeatureTiles
// (what you get) and SecondCta (sign up). The point is to lower the
// "what am I committing to?" friction: visitors who don't yet trust
// the product see exactly the first thirty minutes of using it.
//
// Voice: concrete, second-person sparingly, no "AI-powered", no fake
// effortlessness (BRAND.md §Voice). No em-dashes in customer copy.
//
// Layout: numbered three-up grid, mirrors FeatureTiles spacing so the
// two sections read as a pair. Each step carries a plain "Step N" label
// (the order is information here, so the number stays), set as ordinary
// text rather than a decorative uppercase eyebrow.
//
// Step copy lives in howItWorksData.ts so the landing page's JSON-LD
// HowTo block can share the same source without drifting.
import { howItWorksSteps as steps } from "./howItWorksData";

export default function HowItWorks() {
  return (
    <section
      aria-label="How The Better Decision works"
      className="mx-auto max-w-6xl px-6 py-20 lg:px-10 lg:py-24"
    >
      <div className="mb-10 max-w-2xl">
        <h2 className="font-display text-3xl font-semibold leading-tight text-text-primary lg:text-4xl">
          Thirty minutes to the first calm view of your money.
        </h2>
      </div>
      <ol className="grid gap-6 md:grid-cols-3 lg:gap-8">
        {steps.map((step, i) => (
          <li
            key={step.title}
            className="rounded-xl border border-border bg-surface p-6"
          >
            <div className="mb-3 font-display text-sm font-semibold text-accent">
              Step {i + 1}
            </div>
            <h3 className="mb-2 font-display text-lg font-semibold leading-snug text-text-primary">
              {step.title}
            </h3>
            <p className="text-sm leading-relaxed text-text-secondary">
              {step.body}
            </p>
          </li>
        ))}
      </ol>
    </section>
  );
}
