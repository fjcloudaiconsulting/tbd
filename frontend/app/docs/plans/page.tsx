import Link from "next/link";
import type { Metadata } from "next";
import ThemeToggle from "@/components/ui/ThemeToggle";
import BackLink from "@/components/ui/BackLink";
import { pageSocialMeta, siteName } from "@/lib/site";

const description =
  "How the Plans simulation sandbox works: plan types, verdict colors, the math, and the contribution curve.";

export const metadata: Metadata = {
  title: "Plans guide",
  description,
  alternates: {
    canonical: "/docs/plans",
  },
  ...pageSocialMeta({
    title: `Plans guide · ${siteName}`,
    description,
    path: "/docs/plans",
  }),
};

const sections = [
  { id: "what-is-plans", label: "What is Plans?" },
  { id: "verdict-colors", label: "The verdict colors" },
  { id: "math", label: "How the math works" },
  { id: "how-to-use", label: "How to use Plans" },
  { id: "curve", label: "The contribution curve" },
];

export default function PlansDocsPage() {
  return (
    <div className="relative min-h-screen px-4 py-12">
      <ThemeToggle className="absolute right-6 top-6" />
      <article className="mx-auto max-w-2xl">
        <header className="mb-10">
          <BackLink />
          <p className="mt-6 text-xs uppercase tracking-[0.12em] text-text-muted">
            Docs · Plans
          </p>
          <h1 className="mt-2 font-display text-3xl font-semibold text-text-primary">
            Plans guide
          </h1>
          <p className="mt-2 text-sm text-text-muted">
            How to model a trip, a purchase, retirement, or a custom life
            event. Plans is read-only: nothing here touches your real
            transactions.
          </p>
          <nav
            aria-label="On this page"
            className="mt-6 rounded-lg border border-border bg-surface p-4"
            data-testid="plans-docs-nav"
          >
            <p className="mb-2 text-xs font-semibold uppercase tracking-[0.12em] text-text-muted">
              On this page
            </p>
            <ul className="grid gap-1.5 text-sm sm:grid-cols-2">
              {sections.map((s) => (
                <li key={s.id}>
                  <a
                    href={`#${s.id}`}
                    className="text-text-secondary hover:text-text-primary"
                  >
                    {s.label}
                  </a>
                </li>
              ))}
            </ul>
          </nav>
        </header>

        <div className="space-y-8 text-text-primary [&_h2]:font-display [&_h2]:text-xl [&_h2]:font-semibold [&_h2]:mb-3 [&_h2]:mt-8 [&_h3]:font-display [&_h3]:text-base [&_h3]:font-semibold [&_h3]:mb-2 [&_h3]:mt-6 [&_p]:text-sm [&_p]:leading-relaxed [&_p]:text-text-secondary [&_ul]:list-disc [&_ul]:pl-5 [&_ul]:text-sm [&_ul]:leading-relaxed [&_ul]:text-text-secondary [&_ol]:list-decimal [&_ol]:pl-5 [&_ol]:text-sm [&_ol]:leading-relaxed [&_ol]:text-text-secondary [&_li]:mt-1 [&_code]:rounded [&_code]:bg-surface [&_code]:px-1 [&_code]:py-0.5 [&_code]:text-xs">
          <section data-testid="plans-docs-section-what">
            <h2 id="what-is-plans">What is Plans?</h2>
            <p>
              Plans is a simulation sandbox. Nothing here touches your
              real transactions, accounts, or budgets. You can model
              life events to see how they play out month by month before
              you commit to anything.
            </p>
            <p>
              There are four templates:
            </p>
            <ul>
              <li>
                <strong>Trip.</strong> Model a single trip's cost on a
                start date (transport plus accommodation per night plus
                a daily budget multiplied by duration). The cost lands
                in one dip; the rest of the horizon is just your
                regular cashflow.
              </li>
              <li>
                <strong>Purchase.</strong> Model a one-off purchase
                (car, house deposit, appliance). The down payment lands
                on the target date.
              </li>
              <li>
                <strong>Retirement.</strong> Model long-horizon savings.
                Compound interest, monthly contributions, optional
                step-function contribution curve, and an inflation-
                adjusted real-terms overlay on the chart.
              </li>
              <li>
                <strong>Custom.</strong> Reserved for a later release.
                A bring-your-own-events editor for plans that don't fit
                the three templates.
              </li>
            </ul>
          </section>

          <section data-testid="plans-docs-section-verdict">
            <h2 id="verdict-colors">The verdict colors</h2>
            <p>
              Every simulated plan gets a verdict: green, yellow, or
              red. The verdict reads what the chart shows.
            </p>
            <ul>
              <li>
                <strong>Green.</strong> Your accounts stay healthy and
                the plan's end balance is at least 80 percent of where
                it started (or, for retirement, at least the target).
              </li>
              <li>
                <strong>Yellow.</strong> Either you'll briefly dip below
                zero on an account, or the plan eats more than 20
                percent of your starting net worth. Doable, but not
                comfortable.
              </li>
              <li>
                <strong>Red.</strong> The plan causes an extended cash
                crunch, or (for retirement) you'll fall more than 15
                percent short of your target. Tune the params and
                re-simulate.
              </li>
            </ul>
            <p>
              Below the verdict, the projection panel lists alerts (any
              month an account dips below zero) and suggestions
              ("raise monthly contribution by 200 to close the gap").
              Suggestions are advisory, never automatic; the plan only
              changes when you change the params.
            </p>
          </section>

          <section data-testid="plans-docs-section-math">
            <h2 id="math">How the math works</h2>

            <h3>Trip and Purchase</h3>
            <p>
              We project month by month. We start with your account
              balances as of today, apply your recurring income and
              expenses plus current budget pace, and drop in the plan's
              cashflow events on their scheduled months. For a trip,
              that's a single dip on the start month equal to transport
              plus (accommodation per night times duration) plus (daily
              budget times duration). For a purchase, it's the down
              payment on the target date.
            </p>

            <h3>Retirement</h3>
            <p>
              Retirement uses compound interest, applied monthly. Each
              month the balance grows by your annual return divided by
              12, and your monthly contribution is added on top.
            </p>
            <p>
              We also track the same balance in real (inflation-
              adjusted) terms so the chart's red dashed line tells you
              what your future money would be worth in today's pounds,
              euros, or dollars. The formula:
            </p>
            <p>
              <code>
                real_balance = nominal_balance / ((1 + inflation_rate) ^
                years)
              </code>
            </p>
            <p>
              Both annual return and annual inflation are expressed as
              percentages and converted to monthly factors internally.
              Long-term stock market average return is around 6 to 8
              percent; the Eurozone inflation target is 2 percent and
              the 30-year historical average is around 2.5 to 3
              percent. Your own assumptions are what matters.
            </p>

            <h3>Smooth with regression</h3>
            <p>
              When the engine sees enough recent history, it can fit a
              straight line through your last 12 months of cashflow and
              use the trend instead of the raw recurring numbers. This
              is helpful when your income has been growing or your
              spending has shifted, and you don't want a one-off month
              skewing the projection. The projection panel shows a
              "Trend-adjusted" badge when this kicks in.
            </p>
          </section>

          <section data-testid="plans-docs-section-howto">
            <h2 id="how-to-use">How to use Plans</h2>
            <ol>
              <li>
                Click <strong>+ New plan</strong> and pick a template.
              </li>
              <li>
                Fill in the params. The chart re-simulates as you type
                (about 400 ms after you stop). Inline validation
                errors (a curve row missing its start date, for
                example) skip the re-simulate until you fix them.
              </li>
              <li>
                Read the verdict and the suggestions below the chart.
              </li>
              <li>
                Tweak params (raise the monthly contribution, push the
                retirement date out, lower the trip's daily budget) and
                watch the chart respond.
              </li>
              <li>
                Save the plan. It stays in your list under
                <code>/plans</code>. You can come back later, edit it,
                re-simulate.
              </li>
              <li>
                When you have two or more plans, the Compare plans
                surface lets you put up to three side by side on the
                same chart.
              </li>
            </ol>
            <p>
              The right pane shows the projection; the left pane is the
              editor. The two stay in sync on every keystroke. Use the
              Re-simulate button if you want to force a fresh run after
              switching accounts or after a recurring template change.
            </p>
          </section>

          <section data-testid="plans-docs-section-curve">
            <h2 id="curve">The contribution curve (step function)</h2>
            <p>
              Most people don't save a flat amount across 30 years.
              Maybe you save 500 a month now but plan to bump to 1,000
              a month once your mortgage is paid off, then 1,500 a
              month when the kids leave school. The contribution curve
              is how you model that.
            </p>
            <p>
              Each row in the curve table sets a new monthly
              contribution starting on a given date. Rows must be in
              chronological order. The base contribution applies before
              the first row's date.
            </p>
            <h3>Worked example</h3>
            <p>
              Current age 30, retirement at 65, base monthly
              contribution 500. Add curve rows:
            </p>
            <ul>
              <li>From age 40 (year + 10): 800 per month.</li>
              <li>From age 50 (year + 20): 1,200 per month.</li>
            </ul>
            <p>The projection then runs:</p>
            <ul>
              <li>From age 30 to age 40: 500 per month (base).</li>
              <li>From age 40 to age 50: 800 per month.</li>
              <li>From age 50 to age 65: 1,200 per month.</li>
            </ul>
            <p>
              If you fill in a curve row's amount but not its date, the
              form shows an inline validation error and the auto re-
              simulate pauses until you fix it. The error is intentional;
              it stops a half-typed row from getting saved against your
              plan.
            </p>
          </section>
        </div>

        <footer className="mt-12 border-t border-border pt-6 text-xs text-text-muted">
          See also:{" "}
          <Link href="/docs" className="underline hover:text-text-primary">
            Docs home
          </Link>{" "}
          ·{" "}
          <Link href="/plans" className="underline hover:text-text-primary">
            Plans
          </Link>
        </footer>
      </article>
    </div>
  );
}
