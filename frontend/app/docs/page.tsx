import Link from "next/link";
import type { Metadata } from "next";
import ThemeToggle from "@/components/ui/ThemeToggle";
import BackLink from "@/components/ui/BackLink";
import CookiePreferencesButton from "@/components/landing/CookiePreferencesButton";
import { readNonce } from "@/lib/nonce";
import { apexCanonical, apexUrl, pageSocialMeta, siteName } from "@/lib/site";

const description =
  "Rough user manual for The Better Decision: core concepts, common workflows, and admin tasks.";

// Canonicalize to the apex host (served on both apex and app subdomain).
export const metadata: Metadata = {
  title: "Docs",
  description,
  alternates: {
    canonical: apexCanonical("/docs"),
  },
  ...pageSocialMeta({
    title: `Docs · ${siteName}`,
    description,
    path: apexCanonical("/docs"),
  }),
  robots: { index: true, follow: true },
};

// Structured data for rich results + AI-engine entity resolution.
// Reuse the SAME Organization @id the landing page declares so the
// graph links: this doc resolves to the one canonical Organization
// node. URLs point at the apex (the canonical home for shared pages).
// We omit author / datePublished — there is no truthful value for them.
// isPartOf points at the WebSite node (schema.org expects a CreativeWork
// there, not the Organization); publisher stays the Organization. Both
// @ids match the landing page's nodes so the graph resolves to one site.
const orgId = `${apexUrl}/#organization`;
const websiteId = `${apexUrl}/#website`;

const techArticleLd = {
  "@context": "https://schema.org",
  "@type": "TechArticle",
  headline: "The Better Decision docs",
  description,
  url: apexCanonical("/docs"),
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
  ],
};

const structuredData = [techArticleLd, breadcrumbLd];

const sections = [
  { id: "overview", label: "Overview" },
  { id: "core-concepts", label: "Core concepts" },
  { id: "common-workflows", label: "Common workflows" },
  { id: "forecasts", label: "Forecasts: planning vs projecting" },
  { id: "admin-workflows", label: "Admin workflows" },
  { id: "system-health", label: "System health" },
  { id: "dashboard", label: "Dashboard" },
  { id: "transactions", label: "Transactions" },
  { id: "transfers", label: "Transfers and paying a credit card" },
  { id: "recurring", label: "Recurring transactions" },
  { id: "accounts", label: "Accounts" },
  { id: "categories", label: "Categories" },
  { id: "budgets", label: "Budgets" },
  { id: "forecast-plans", label: "Forecast Plans" },
  { id: "ai-features", label: "AI features" },
  { id: "admin", label: "Admin" },
  { id: "whats-next", label: "What's next" },
];

export default async function DocsPage() {
  // Per-request CSP nonce so the JSON-LD inline scripts pass the strict
  // prod CSP. readNonce() returns "" on the apex static export (no
  // request context), so we conditionally spread the prop — same pattern
  // app/page.tsx uses.
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
          <h1 className="mt-6 font-display text-3xl font-semibold text-text-primary">
            Docs
          </h1>
          <p className="mt-2 text-sm text-text-muted">
            A short, in-app manual. Rough scaffolding, will grow over
            time.
          </p>
          <nav
            aria-label="On this page"
            className="mt-6 rounded-lg border border-border bg-surface p-4"
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

        <div className="space-y-8 text-text-primary [&_h2]:font-display [&_h2]:text-xl [&_h2]:font-semibold [&_h2]:mb-3 [&_h2]:mt-8 [&_h3]:font-display [&_h3]:text-base [&_h3]:font-semibold [&_h3]:mb-2 [&_h3]:mt-6 [&_p]:text-sm [&_p]:leading-relaxed [&_p]:text-text-secondary [&_ul]:list-disc [&_ul]:pl-5 [&_ul]:text-sm [&_ul]:leading-relaxed [&_ul]:text-text-secondary [&_ol]:list-decimal [&_ol]:pl-5 [&_ol]:text-sm [&_ol]:leading-relaxed [&_ol]:text-text-secondary [&_li]:mt-1">
          <section>
            <h2 id="overview">What is The Better Decision</h2>
            <p>
              The Better Decision is a personal finance
              app for households and individuals. It tracks accounts,
              transactions, budgets, and forecasts, all scoped to your
              organization. Categories are the backbone of the model:
              budgets and forecasts ride on top of them, and most
              reporting is grouped by category. The app is pre-launch,
              so copy may be rough and some flows are still evolving.
            </p>
          </section>

          <section>
            <h2 id="core-concepts">Core concepts</h2>
            <ul>
              <li>
                <strong>Organizations.</strong> Every user belongs to an
                organization. All data (accounts, transactions,
                categories, plans) is scoped to that organization.
              </li>
              <li>
                <strong>Members and roles.</strong> Within an org, users
                are owner, admin, or member. There is also a platform
                superadmin role for operators of the service.
              </li>
              <li>
                <strong>Accounts.</strong> Bank accounts, credit cards,
                cash, savings. Each account has a type and a balance
                derived from its transactions.
              </li>
              <li>
                <strong>Categories.</strong> Hierarchical: master
                categories group related subcategories. Each category
                has a type (income, expense, or both) which constrains
                where it can be used.
              </li>
              <li>
                <strong>Transactions.</strong> Every transaction has a
                purchase date and a settled date. The settled date
                determines which billing period the transaction belongs
                to, which matters for credit cards where the purchase
                and the statement fall in different months.
              </li>
              <li>
                <strong>Recurring transactions.</strong> Templates that
                fire on a cadence (monthly, weekly, biweekly, and so
                on) and generate transactions automatically.
              </li>
              <li>
                <strong>Forecasts and budgets.</strong> Budgets are the
                current-period control surface. Forecast plans are
                forward-looking projections per category, used by the
                dashboard to show what the period is on track for.
              </li>
              <li>
                <strong>Plans.</strong> A separate simulation sandbox
                for one-off life events (trip, purchase, retirement).
                Plans never touch your real transactions. See the{" "}
                <Link
                  href="/docs/plans"
                  className="underline hover:text-text-primary"
                  data-testid="docs-plans-link"
                >
                  Plans guide
                </Link>{" "}
                for the full mental model.
              </li>
            </ul>
          </section>

          <section>
            <h2 id="common-workflows">Common workflows</h2>

            <h3>Importing a CSV</h3>
            <p>
              From the Import page, upload a CSV from your bank. The
              app parses the file, suggests categories where it can
              (auto-categorization), and shows a preview before
              anything is written. Confirm the preview to commit the
              rows as transactions.
            </p>

            <h3>Editing a transaction inline</h3>
            <p>
              On the Transactions page, click a row to expand the
              inline editor. You can change the amount, dates,
              category, and notes without leaving the list.
            </p>

            <h3>Promoting a transaction to recurring</h3>
            <p>
              When editing a transaction, the inline row exposes a
              &#34;Promote to recurring&#34; action that turns the transaction
              into a recurring template, so the same charge generates
              automatically next period.
            </p>
            <p>
              Removing a recurring template only drops future,
              un-materialized occurrences. Past occurrences stay in
              your transaction history because they are real money
              movements, so balances are not affected. This is
              intentional: the template only schedules what has not
              happened yet.
            </p>
            <p>
              Transactions generated from a recurring template are
              independent copies. Editing the name or category of any
              one of them updates the template and every upcoming (not
              yet settled) occurrence, so the series stays consistent
              going forward. Amounts and dates stay per occurrence, so
              you can adjust a single month without changing the rest.
              Settled past entries are never rewritten. Deleting a
              single transaction never removes other occurrences. Only
              stopping or deleting the recurring template removes its
              pending future occurrences, and settled history is always
              kept.
            </p>

            <h3>Marking transactions as a transfer pair</h3>
            <p>
              When money moves between two of your own accounts, you
              can either record the pair from the start (Add transfer)
              or convert an existing transaction by selecting its
              counterparty. The pair is then excluded from
              category-level spending so transfers do not double-count.
            </p>

            <h3>Setting up a forecast plan</h3>
            <p>
              From the Forecast Plans page, create a plan for the
              current or upcoming period. Categories pull from past
              activity and recurring templates as starting points;
              edit the per-category amount to match your expectation
              and save.
            </p>

            <h3>Reviewing the dashboard verdict</h3>
            <p>
              The dashboard summarizes the period with an On Track
              verdict that anchors on what has actually settled, not
              just what is projected. Below the verdict, a Forecast
              card shows your expected month-end balance per account
              (current balance plus pending items in the period), and
              a Forecast by Category card highlights how planned and
              actual compare for your top expense categories.
            </p>
            <p>
              The Dashboard intentionally stays light on technical
              detail. For variance numbers, source attribution, the
              planned vs actual chart, and the refresh-from-sources
              control, open the Forecast Plans page and turn on Show
              details.
            </p>
          </section>

          <section>
            <h2 id="forecasts">Forecasts: planning vs projecting</h2>
            <p>
              The Forecasts area can feel confusing at first because four
              ideas overlap: the plan, the projection, the variance, and
              the two buttons that fill the plan in. Here is each one in
              the simplest words.
            </p>
            <p>
              Where you see what: the Dashboard answers household
              questions visually (are we on track, where will we end the
              month, which accounts and categories explain that). The
              Forecast Plans page is where you build and adjust the
              plan itself. Forecast Plans starts in a simple view and
              has a Show details toggle that reveals variance numbers,
              source attribution, the planned vs actual chart, and the
              refresh-from-sources control when you need them.
            </p>

            <h3>The plan is your wish list</h3>
            <p>
              A plan is a wish list with numbers. You write down what you
              want to spend on each category for the period. For example,
              &#34;I want to spend 600 on groceries this month and 80 on
              coffee.&#34; That is the plan. Saving the plan does not move
              any money. It just records your intention.
            </p>

            <h3>Auto-populate fills the wish list for you</h3>
            <p>
              When you create a brand new plan, the wish list is empty.
              Click <strong>Auto-populate</strong> and the app fills it
              in for you. It looks at three things, in order, for each
              master category:
            </p>
            <ol>
              <li>
                <strong>Recurring bills</strong> that will fire in the
                period (rent, Netflix, salary). Their amounts are added
                up.
              </li>
              <li>
                <strong>Your last 3 months of activity</strong>, averaged.
                Used for categories that don&#39;t have a recurring bill.
              </li>
              <li>
                <strong>Whatever you&#39;ve already booked in this period</strong>,
                blended in with the past 3 months so the suggestion reflects
                your most recent reality. A category can be seeded from
                current-period activity alone, even if it has no 3-month
                history.
              </li>
            </ol>
            <p>
              Each row in the plan shows where its number came from: a
              recurring template, your history (labeled &#34;Auto&#34;), or a
              manual edit you made.
            </p>

            <h3>Variance: did you stick to the wish list</h3>
            <p>
              Variance compares your plan to what actually happened.
            </p>
            <ul>
              <li>
                You planned 600 for groceries and spent 400 so far.
                Variance is 200 under plan, shown in green. You have
                room to spare.
              </li>
              <li>
                You planned 600 for groceries and spent 700. Variance
                is 100 over plan, shown in red. You went past the
                number you wrote down.
              </li>
              <li>
                For income, the colors flip. Earning more than you
                planned is good (green), earning less is amber or red.
              </li>
            </ul>

            <h3>Projected: the app&#39;s best guess for end of month</h3>
            <p>
              The Projected number on the dashboard is a totally
              different idea from the plan. The plan is what you want
              to spend. Projected is what the app thinks will actually
              happen by the end of the month, given everything it can
              already see. It is the sum of:
            </p>
            <ul>
              <li>
                <strong>Settled</strong>: what has already cleared.
              </li>
              <li>
                <strong>Pending</strong>: charges that have been booked
                but not yet cleared (a card swipe waiting on the
                statement, a manual upcoming expense you typed in).
              </li>
              <li>
                <strong>Scheduled recurring</strong>: every recurring
                bill that will fire from now until the end of the
                period.
              </li>
            </ul>
            <p>
              <strong>
                Projected is not Planned Income minus Planned Expenses.
              </strong>{" "}
              It can be higher than your plan if pending charges or
              upcoming recurring bills are bigger than you expected, or
              lower if life has been quiet and not many bills are
              scheduled. Treat Projected as a heads-up, not a verdict.
              The dashboard&#39;s On Track verdict reads from what has
              actually settled, so a noisy projection won&#39;t alarm you
              before any money has moved.
            </p>

            <h3>Refresh from sources: re-check after life changes</h3>
            <p>
              Auto-populate is for the very first fill. Refresh from
              sources is for later. Use it when something about your
              setup has changed since you first built the plan. Maybe
              you added a new subscription, ended an old one, edited
              the amount of a recurring bill, or imported new
              transactions that should feed the average.
            </p>
            <p>
              Refresh drops every row whose source is Recurring or Auto
              (the rows the system filled in) and re-runs Auto-populate
              against today&#39;s templates and history. Rows you typed or
              edited yourself are kept untouched. Categories that have
              shown up since the first populate will be added in this
              pass too.
            </p>
            <p>
              Refresh from sources is part of the details view on the
              Forecast Plans page (turn on Show details to see it).
              On a draft plan, Refresh runs immediately. On a finalized
              plan, Refresh opens an Edit and refresh confirmation that
              reverts the plan to draft, refreshes the auto-generated
              rows, and keeps the lines you added or edited yourself.
              If the refresh step fails after the revert, the plan is
              left in draft and the error is shown so you can retry or
              continue editing manually.
            </p>
          </section>

          <section>
            <h2 id="admin-workflows">Admin workflows</h2>
            <ul>
              <li>
                <strong>Renaming the org.</strong> Owners can rename
                the organization from Settings. Names are unique
                case-insensitively across the platform, and the change
                is captured in the audit log.
              </li>
              <li>
                <strong>Inviting members.</strong> Owners and admins
                can invite people by email. Invitees receive a link,
                set up an account, and join the org with the role
                chosen at invite time.
              </li>
              <li>
                <strong>Managing roles and permissions.</strong>{" "}
                Superadmins can review and edit platform roles and the
                permissions attached to each, from the Admin area.
              </li>
              <li>
                <strong>Viewing the audit log.</strong> Superadmins can
                browse the audit log to see security and admin events
                (logins, role changes, org renames, deletions).
              </li>
              <li>
                <strong>Toggling org settings.</strong> Settings holds
                org-level preferences (billing cycle, defaults) that
                influence how periods are computed and how new
                transactions are filed.
              </li>
            </ul>
          </section>

          <section>
            <h2 id="system-health">System health</h2>
            <p>
              When something feels off (slow loads, missing data,
              dashboard not updating), superadmins can check the Admin
              dashboard&#39;s System health card. It reports the live
              status of the database and Redis, including latency.
              Failures there usually explain whatever the rest of the
              app is doing.
            </p>
          </section>

          <section>
            <h2 id="dashboard">Dashboard</h2>
            <p>
              The Dashboard is the household summary screen. The On Track
              hero tile reads from what has actually settled in the
              current billing period, so it stays calm even when pending
              charges or upcoming recurring bills make the projection
              jumpy. Below the hero, the Accounts strip shows balance
              plus pending per account, and a Forecast card shows the
              expected month-end balance for each account. Three cards
              underneath summarize spending by category, budget
              progress, and forecast by category. Use the period nav at
              the top to step through past or future periods; the donut
              and bars are filterable by category. For variance numbers,
              source attribution, and the planned-vs-actual chart, open
              Forecast Plans and turn on Show details.
            </p>
            <p>
              The dashboard is also customizable: enter Customize mode to
              rearrange, resize, add, or remove tiles, including charts
              cloned from your saved reports.{" "}
              <Link
                href="/docs/dashboard"
                className="underline hover:text-text-primary"
                data-testid="docs-dashboard-link"
              >
                See the full dashboard guide
              </Link>{" "}
              for rearranging, the widget picker, adding a widget from a
              report, and the Recent Transactions controls.
            </p>
          </section>

          <section>
            <h2 id="transactions">Transactions</h2>
            <p>
              The Transactions page lists every booked row in the
              selected billing period. Click a row to expand the inline
              editor, where you can change description, amount, dates,
              category, status, and notes without leaving the list. From
              the same row you can promote a transaction to a recurring
              template, convert it into a transfer leg by selecting its
              counterparty account, or attach tags. Use the toolbar to
              filter by status, account, or category, and to sort the
              columns. Imports land here as a preview first, so nothing
              is committed until you confirm.
            </p>
          </section>

          <section>
            <h2 id="transfers">Transfers and paying a credit card</h2>
            <p>
              A transfer is money moving between two of your own
              accounts (checking to savings, checking to credit card,
              and so on). Transfers are not income or expense: they do
              not change your net position, so the app tracks them as a
              paired movement and keeps them out of category-level
              spending totals.
            </p>

            <h3>How transfers are categorized</h3>
            <p>
              Both legs of a transfer share one category. That category
              has to be transfer-compatible (its type is &#34;both&#34;), so it
              works on the outgoing leg as well as the incoming leg. The
              transfer form filters the category picker to just these
              transfer-compatible categories. If you leave the picker
              empty, the app uses the built-in Transfer category, which
              is enough for most setups.
            </p>

            <h3>Paying a credit card bill</h3>
            <p>
              A credit card payment is a transfer, not an expense. The
              spending already happened when you swiped the card. Paying
              the statement moves cash from your bank account to your
              credit card account, which clears the card balance back
              toward zero.
            </p>
            <ul>
              <li>
                Card purchases (groceries, gas, restaurants) stay as
                regular expenses on the credit card account. Categorize
                them the way you would any other purchase.
              </li>
              <li>
                Paying the statement is a single transfer from your
                bank account to the credit card account. Use Add
                transfer (or Mark as transfer on an existing pair).
              </li>
              <li>
                If you want to see card payments broken out separately
                from generic transfers, pick Credit Card Payment (or
                create another transfer-compatible category) from the
                transfer form. Both legs will use that category. The
                picker shows only categories that work on both legs,
                so expense-only categories like Debt Repayment or
                Credit Cards cannot be used on a transfer.
              </li>
            </ul>
            <p>
              Heads up: an expense-only category (for example &#34;Debt
              Repayment / Credit Cards&#34;) cannot be used on a transfer.
              The picker hides those choices on the transfer form, and
              the backend rejects them on the API, since an
              expense-only category cannot apply to the incoming leg
              that lands on the credit card account.
            </p>
          </section>

          <section>
            <h2 id="recurring">Recurring transactions</h2>
            <p>
              The Recurring page lists every template you have set up,
              split into Active and Paused. Templates are created by
              promoting an existing transaction (open the row on the
              Transactions page and use &#34;Promote to recurring&#34;); they
              are not authored directly here.
            </p>
            <h3>Generate this period</h3>
            <p>
              &#34;Generate this period&#34; fills the current billing cycle
              with this period&#39;s recurring transactions, materializing
              an occurrence for every active recurring template that
              falls within the cycle. Items due later in the period
              appear as pending until their date arrives, so you can
              forecast a credit card statement that has not closed yet.
              Run it whenever you want this period&#39;s recurring rows to
              appear; they are not created automatically. Stopping a
              template removes its remaining pending future rows; settled
              past rows are kept because they are real money movements.
            </p>
          </section>

          <section>
            <h2 id="accounts">Accounts</h2>
            <p>
              Accounts represent your real-world money containers: bank
              accounts, credit cards, cash, savings. Each account has a
              type, a currency, and a balance derived from its
              transactions plus pending items. Credit-card accounts also
              carry a close day, which drives the billing period for
              charges on that card. From here you can add new accounts,
              set the default one (it shows first on the Dashboard
              strip), close an account, or post a manual balance
              adjustment when the in-app total drifts from the bank&#39;s.
            </p>
          </section>

          <section>
            <h2 id="categories">Categories</h2>
            <p>
              Categories are the backbone of budgets and forecasts. They
              are hierarchical: master categories group related
              subcategories, and each category has a type (income,
              expense, or both) that constrains where it can be used.
              Edit mode unlocks batch selection so you can move or
              delete multiple subcategories at once. Renaming is
              live-reference, so existing transactions update
              automatically. Deleting a category that is in use prompts
              you to migrate its rows to another category in the same
              type. Master categories cannot be removed while they have
              children.
            </p>
          </section>

          <section>
            <h2 id="budgets">Budgets</h2>
            <p>
              Budgets are the current-period control surface: a number
              per category that represents the cap you want to spend
              against for this period. The bar chart on the Budgets page
              colors each row by utilization (under, near, over), and
              the Dashboard&#39;s Budget Progress card pulls the same
              numbers. Budgets are scoped to the open billing period.
              For future periods, use Forecast Plans instead, and click
              From Forecast to seed the current period&#39;s budgets from
              the matching plan.
            </p>
          </section>

          <section>
            <h2 id="forecast-plans">Forecast Plans</h2>
            <p>
              Forecast Plans is where you build and adjust the wish list
              of what you expect to spend and earn per category for a
              billing period. Auto-populate fills the plan from
              recurring bills, your last 3 months of activity, and
              anything already booked in the period. The Show details
              toggle reveals variance, source attribution, the planned
              vs actual chart, and Refresh from sources, which re-runs
              auto-population while preserving rows you typed yourself.
              See the Forecasts section above for the full mental model
              of plan vs projected vs variance.
            </p>
          </section>

          <section>
            <h2 id="ai-features">AI features</h2>
            <p>
              The Better Decision has three optional AI helpers. They are off by default and
              only run when an org admin has connected an AI provider, because each call uses
              your provider account and its tokens (which cost money).
            </p>
            <h3>Setting up a provider</h3>
            <p>
              An org admin opens Settings, then AI providers, and adds a key for OpenAI,
              Anthropic, Ollama, or an OpenAI-compatible endpoint. Keys are encrypted at rest
              and never shown again after you save them. Until a provider is connected, each AI
              action shows a &#34;Set up AI&#34; prompt instead of running.
            </p>
            <h3>Auto-categorize (Transactions)</h3>
            <p>
              When you edit a transaction, &#34;Suggest category&#34; asks the AI for a category based
              on the description and amount. It only fills the picker; nothing is saved until
              you save the transaction.
            </p>
            <h3>Refine forecast with AI (Dashboard)</h3>
            <p>
              &#34;Refine forecast with AI&#34; layers AI-detected seasonal patterns on top of your
              baseline forecast. You choose how much history and how many categories to analyze
              and see the estimated cost before confirming. The result is a preview you can
              revert; it does not change your saved data.
            </p>
            <h3>Suggest rebalance (Budgets)</h3>
            <p>
              On the current period, &#34;Suggest rebalance&#34; asks the AI to propose budget changes
              across categories based on recent spending. You accept or skip each suggestion;
              nothing changes until you apply it.
            </p>
          </section>

          <section>
            <h2 id="admin">Admin</h2>
            <p>
              The Admin area is the platform-ops hub, visible only to
              users with platform permissions. The header summarizes
              live database and Redis health; the totals strip tracks
              orgs, users, active subscriptions, and recent signups. The
              tiles below open the Organizations browser, the Audit log,
              and the Roles editor. Most destructive actions
              (subscription overrides, org deletes, tenant resets) are
              captured in the audit log automatically.
            </p>
          </section>

          <section>
            <h2 id="whats-next">What&#39;s next, known gaps</h2>
            <p>
              This page is rough scaffolding, not a finished manual.
              Expect more depth here over time, with screenshots,
              edge-case notes, and better cross-linking. Areas that
              are intentionally light right now:
            </p>
            <ul>
              <li>Reporting beyond the dashboard summary.</li>
              <li>
                A side-by-side comparison page for budgets and
                forecasts.
              </li>
              <li>Onboarding for first-time users.</li>
            </ul>
          </section>
        </div>

        <footer className="mt-12 flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-border pt-6 text-xs text-text-muted">
          <span>
            See also:{" "}
            <Link href="/privacy" className="underline hover:text-text-primary">
              Privacy
            </Link>{" "}
            ·{" "}
            <Link href="/terms" className="underline hover:text-text-primary">
              Terms
            </Link>
          </span>
          <CookiePreferencesButton />
        </footer>
      </article>
    </div>
  );
}
