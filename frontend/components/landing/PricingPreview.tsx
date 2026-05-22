import Link from "next/link";
import { signupHref } from "@/lib/links";
import { btnPrimary, btnSecondary } from "@/lib/styles";

// PricingPreview — three-tier cards for Free / Pro / Team.
//
// IMPORTANT: BILLING_UI_ENABLED is currently false (PR #339 / 2026-05-22)
// and the payment platform is not wired. So this section MUST NOT claim
// users can pay today. Each paid tier carries a "Coming soon" badge and
// the CTA reads "Join the waitlist" rather than "Upgrade". The Free
// tier is the only path to live signup right now, and it links to
// /register through the canonical signupHref() (which honors apex /
// app-host build-target routing).
//
// When billing wires up, this file's `comingSoon: true` flags flip to
// false, the waitlist CTA swaps to a real checkout link, and the
// `comingSoonBadge` block is removed. No other change needed.
//
// Open architect question: the prices (€9 Pro, €19 Team) are
// placeholders for the design preview. Actual price ladder needs
// product decision before launch — flagged in the PR body.

const tiers = [
  {
    name: "Free",
    price: "€0",
    cadence: "forever",
    description: "Personal use, one household. The full app, no time limit.",
    features: [
      "Unlimited transactions",
      "All categories and budgets",
      "CSV import",
      "Reports v1",
      "Up to 2 accounts",
    ],
    cta: "Get started free",
    href: signupHref(),
    style: "primary" as const,
    comingSoon: false,
    highlighted: false,
  },
  {
    name: "Pro",
    price: "€9",
    cadence: "per month",
    description: "For people who plan ahead. Forecasts, scenarios, and AI help.",
    features: [
      "Everything in Free",
      "Unlimited accounts",
      "Scenario planning (retirement, trips, purchases)",
      "Reports v2 custom canvas",
      "AI assistant (Pro tier)",
      "Priority email support",
    ],
    cta: "Join the waitlist",
    href: signupHref(),
    style: "primary" as const,
    comingSoon: true,
    highlighted: true,
  },
  {
    name: "Team",
    price: "€19",
    cadence: "per month",
    description: "Shared household or small finance team. Roles and audit trail.",
    features: [
      "Everything in Pro",
      "Multiple members per org",
      "Role-based permissions",
      "Audit log of sensitive actions",
      "Org-level data export",
      "Priority response",
    ],
    cta: "Join the waitlist",
    href: signupHref(),
    style: "secondary" as const,
    comingSoon: true,
    highlighted: false,
  },
];

export default function PricingPreview() {
  return (
    <section
      id="pricing"
      aria-labelledby="pricing-heading"
      className="mx-auto max-w-6xl px-6 py-20 lg:px-10 lg:py-24"
    >
      <div className="mb-12 max-w-2xl">
        <p className="mb-3 font-display text-xs font-semibold uppercase tracking-[0.18em] text-text-muted">
          Pricing preview
        </p>
        <h2
          id="pricing-heading"
          className="font-display text-3xl font-semibold leading-tight text-text-primary lg:text-4xl"
        >
          Simple pricing. No surprises.
        </h2>
        <p className="mt-4 text-base leading-relaxed text-text-secondary">
          The Free tier is live today. Pro and Team open as the payment
          platform wires up. Join the waitlist and we will email you
          when paid plans go live.
        </p>
      </div>
      <div className="grid gap-6 lg:grid-cols-3 lg:gap-8">
        {tiers.map((tier) => (
          <article
            key={tier.name}
            aria-labelledby={`tier-${tier.name.toLowerCase()}-name`}
            className={`relative flex flex-col rounded-xl border p-6 lg:p-7 motion-safe:animate-fade-in-up ${
              tier.highlighted
                ? "border-accent bg-surface-raised shadow-card"
                : "border-border bg-surface"
            }`}
          >
            {tier.highlighted ? (
              <span className="absolute -top-3 left-6 rounded-full bg-accent px-3 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-accent-text">
                Most popular
              </span>
            ) : null}
            <header className="mb-5">
              <h3
                id={`tier-${tier.name.toLowerCase()}-name`}
                className="font-display text-xl font-semibold text-text-primary"
              >
                {tier.name}
              </h3>
              <div className="mt-3 flex items-baseline gap-2">
                <span className="font-display text-4xl font-semibold tabular-nums text-text-primary">
                  {tier.price}
                </span>
                <span className="text-sm text-text-muted">{tier.cadence}</span>
              </div>
              <p className="mt-3 text-sm leading-relaxed text-text-secondary">
                {tier.description}
              </p>
              {tier.comingSoon ? (
                <p className="mt-3 inline-flex items-center gap-2 rounded-md bg-info-dim px-2.5 py-1 text-[11px] font-medium text-info">
                  <span
                    aria-hidden
                    className="h-1.5 w-1.5 rounded-full bg-info"
                  />
                  Coming soon
                </p>
              ) : null}
            </header>
            <ul className="mb-7 flex-1 space-y-2.5 text-sm text-text-secondary">
              {tier.features.map((f) => (
                <li key={f} className="flex items-start gap-2.5">
                  <CheckGlyph />
                  <span>{f}</span>
                </li>
              ))}
            </ul>
            <Link
              href={tier.href}
              className={`${
                tier.style === "primary" ? btnPrimary : btnSecondary
              } inline-flex w-full items-center justify-center px-4 py-2.5 text-sm`}
              aria-describedby={
                tier.comingSoon
                  ? `tier-${tier.name.toLowerCase()}-coming-soon`
                  : undefined
              }
            >
              {tier.cta}
            </Link>
            {tier.comingSoon ? (
              <p
                id={`tier-${tier.name.toLowerCase()}-coming-soon`}
                className="mt-3 text-center text-[11px] text-text-muted"
              >
                Account stays on Free until paid plans launch.
              </p>
            ) : null}
          </article>
        ))}
      </div>
      <p className="mt-10 text-center text-xs text-text-muted">
        Prices shown in euros. Local currency and VAT handling land with
        the payment platform.
      </p>
    </section>
  );
}

function CheckGlyph() {
  return (
    <svg
      viewBox="0 0 16 16"
      aria-hidden
      className="mt-0.5 h-4 w-4 flex-shrink-0 text-accent"
    >
      <path
        d="M3 8.5l3 3 6.5-7"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
