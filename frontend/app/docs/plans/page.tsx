import Link from "next/link";
import type { Metadata } from "next";
import ThemeToggle from "@/components/ui/ThemeToggle";
import BackLink from "@/components/ui/BackLink";
import { readNonce } from "@/lib/nonce";
import { apexCanonical, apexUrl, pageSocialMeta, siteName } from "@/lib/site";

const description =
  "How the Plans simulation sandbox works: plan types, verdict colors, the math, and the contribution curve.";

// Canonicalize to the apex host (served on both apex and app subdomain).
export const metadata: Metadata = {
  title: "Plans guide",
  description,
  alternates: {
    canonical: apexCanonical("/docs/plans"),
  },
  ...pageSocialMeta({
    title: `Plans guide · ${siteName}`,
    description,
    path: apexCanonical("/docs/plans"),
  }),
  robots: { index: true, follow: true },
};

// Structured data for rich results + AI-engine entity resolution.
// Reuse the SAME Organization @id the landing page declares so the
// graph links to the one canonical Organization node. URLs point at
// the apex. We omit author / datePublished — no truthful value exists.
// isPartOf points at the WebSite node (schema.org expects a CreativeWork,
// not the Organization); publisher stays the Organization. Both @ids
// match the landing page's nodes so the graph resolves to one site.
const orgId = `${apexUrl}/#organization`;
const websiteId = `${apexUrl}/#website`;

const techArticleLd = {
  "@context": "https://schema.org",
  "@type": "TechArticle",
  headline: "Plans guide",
  description,
  url: apexCanonical("/docs/plans"),
  inLanguage: "en",
  isPartOf: { "@id": websiteId },
  publisher: { "@id": orgId },
};

const breadcrumbLd = {
  "@context": "https://schema.org",
  "@type": "BreadcrumbList",
  itemListElement: [
    {
      "@type": "ListItem",
      position: 1,
      name: "Home",
      item: apexCanonical("/"),
    },
    {
      "@type": "ListItem",
      position: 2,
      name: "Docs",
      item: apexCanonical("/docs"),
    },
    {
      "@type": "ListItem",
      position: 3,
      name: "Plans",
      item: apexCanonical("/docs/plans"),
    },
  ],
};

const structuredData = [techArticleLd, breadcrumbLd];

const sections = [
  { id: "what-is-plans", label: "What is Plans?" },
  { id: "verdict-colors", label: "The verdict colors" },
  { id: "math", label: "How the math works" },
  { id: "how-to-use", label: "How to use Plans" },
  { id: "curve", label: "The contribution curve" },
];

export default async function PlansDocsPage() {
  // Per-request CSP nonce so the JSON-LD inline scripts pass the strict
  // prod CSP. readNonce() returns "" on the apex static export (no
  // request context); conditionally spread the prop, same as app/page.tsx.
  const nonce = await readNonce();
  const nonceProp = nonce ? { nonce } : {};
  return (
    <div className="relative min-h-screen px-4 py-12">
      {structuredData.map((block) => (
        <script
          key={block["@type"]}
          type="application/ld+json"
          {...nonceProp}
          dangerouslySetInnerHTML={{
            __html: JSON.stringify(block).replace(/<\/script>/gi, "<\\/script>"),
          }}
        />
      ))}
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
                <strong>Trip.</strong> Model a single trip&#39;s cost on a
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
                <strong>Custom.</strong> A fully editable plan type for
                anything that doesn&#39;t fit the three templates above.
                You add a list of events to the timeline and the
                simulator replays them month by month. Five event types
                are available:
                <ul>
                  <li>
                    <code>income_off</code>: silence your recurring
                    income for a date range (sabbatical, parental leave,
                    a planned career break).
                  </li>
                  <li>
                    <code>expense_off</code>: silence recurring expenses
                    for a date range, optionally scoped to a single
                    category (cancel the gym while travelling).
                  </li>
                  <li>
                    <code>recurring_on</code>: marker for the future
                    exclude-recurring base flag. Currently a documented
                    no-op; the event lands on the plan but the simulator
                    does not yet alter the baseline.
                  </li>
                  <li>
                    <code>one_off_income</code>: a single income lump on
                    a given month (bonus, tax refund, gift).
                  </li>
                  <li>
                    <code>one_off_expense</code>: a single expense lump
                    on a given month (replacement laptop, vet bill).
                  </li>
                </ul>
                Example: plan a 3-month sabbatical by adding an
                <code>income_off</code> from month 6 to month 9 and
                watch the chart show the dip and the recovery once
                income resumes.
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
                the plan&#39;s end balance is at least 80 percent of where
                it started (or, for retirement, at least the target).
              </li>
              <li>
                <strong>Yellow.</strong> Either you&#39;ll briefly dip below
                zero on an account, or the plan eats more than 20
                percent of your starting net worth. Doable, but not
                comfortable.
              </li>
              <li>
                <strong>Red.</strong> The plan causes an extended cash
                crunch, or (for retirement) you&#39;ll fall more than 15
                percent short of your target. Tune the params and
                re-simulate.
              </li>
            </ul>
            <p>
              Below the verdict, the projection panel lists alerts (any
              month an account dips below zero) and suggestions
              (&#34;raise monthly contribution by 200 to close the gap&#34;).
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
              expenses plus current budget pace, and drop in the plan&#39;s
              cashflow events on their scheduled months. For a trip,
              that&#39;s a single dip on the start month equal to transport
              plus (accommodation per night times duration) plus (daily
              budget times duration). For a purchase, it&#39;s the down
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
              adjusted) terms so the chart&#39;s red dashed line tells you
              what your future money would be worth in today&#39;s pounds,
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
              Common assumptions for long-term diversified stock
              portfolios range from 5 to 8 percent annually; check
              your own risk tolerance and historical returns for your
              asset mix. The European Central Bank targets 2 percent
              annual inflation; actual long-run averages vary by
              decade and by country, so use a value that matches your
              assumptions for the period you are projecting. Your own
              assumptions are what matters.
            </p>

            <h3>Smooth with regression</h3>
            <p>
              When the engine sees enough recent history, it can fit a
              straight line through your last 12 months of cashflow and
              use the trend instead of the raw recurring numbers. This
              is helpful when your income has been growing or your
              spending has shifted, and you don&#39;t want a one-off month
              skewing the projection. The projection panel shows a
              &#34;Trend-adjusted&#34; badge when this kicks in.
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
                retirement date out, lower the trip&#39;s daily budget) and
                watch the chart respond.
              </li>
              <li>
                The editor auto-saves as you type (about 400 ms after
                you stop). Your plan stays in your list under
                <code>/plans</code>, ready to revisit and edit later.
                Use the Re-simulate button if you want to refresh the
                chart with your latest changes without waiting for the
                debounce.
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
              Most people don&#39;t save a flat amount across 30 years.
              Maybe you save 500 a month now but plan to bump to 1,000
              a month once your mortgage is paid off, then 1,500 a
              month when the kids leave school. The contribution curve
              is how you model that.
            </p>
            <p>
              Each row in the curve table sets a new monthly
              contribution starting on a given date. Rows must be in
              chronological order. The base contribution applies before
              the first row&#39;s date.
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
              If you fill in a curve row&#39;s amount but not its date, the
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
