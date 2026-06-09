// frontend/components/landing/EverythingInTheApp.tsx
// Compact strip surfacing more shipped features on the highest-traffic page,
// linking to /features. Shipped items only, no roadmap. No em-dashes.
import Link from "next/link";

const items = [
  "Cash-flow forecasting and what-if scenarios",
  "Recurring income and bills",
  "Category budgets and reports",
  "CSV and OFX import",
  "Shared household with roles",
  "Bring-your-own or local AI, with spend caps",
];

export default function EverythingInTheApp() {
  return (
    <section
      aria-label="Everything in the app"
      className="mx-auto max-w-6xl px-6 py-20 lg:px-10 lg:py-24"
    >
      <h2 className="font-display text-3xl font-semibold leading-tight text-text-primary lg:text-4xl">
        Everything in the app
      </h2>
      <ul className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {items.map((item) => (
          <li
            key={item}
            className="rounded-xl border border-border bg-surface p-5 text-sm leading-relaxed text-text-secondary"
          >
            {item}
          </li>
        ))}
      </ul>
      <p className="mt-8 text-sm text-text-muted">
        <Link href="/features" className="underline hover:text-text-primary">
          See every feature
        </Link>{" "}
        ·{" "}
        <Link href="/compare" className="underline hover:text-text-primary">
          Compare with YNAB, PocketSmith, Monarch, and spreadsheets
        </Link>
      </p>
    </section>
  );
}
