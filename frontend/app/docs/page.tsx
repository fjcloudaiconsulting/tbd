import Link from "next/link";
import type { Metadata } from "next";
import ThemeToggle from "@/components/ui/ThemeToggle";
import BackLink from "@/components/ui/BackLink";
import { pageSocialMeta, siteName } from "@/lib/site";

const description =
  "Rough user manual for The Better Decision: core concepts, common workflows, and admin tasks.";

export const metadata: Metadata = {
  title: "Docs",
  description,
  alternates: {
    canonical: "/docs",
  },
  ...pageSocialMeta({
    title: `Docs · ${siteName}`,
    description,
    path: "/docs",
  }),
};

const sections = [
  { id: "overview", label: "Overview" },
  { id: "core-concepts", label: "Core concepts" },
  { id: "common-workflows", label: "Common workflows" },
  { id: "admin-workflows", label: "Admin workflows" },
  { id: "system-health", label: "System health" },
  { id: "whats-next", label: "What's next" },
];

export default function DocsPage() {
  return (
    <div className="relative min-h-screen px-4 py-12">
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
              The Better Decision (codename pfv) is a personal finance
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
              "Promote to recurring" action that turns the transaction
              into a recurring template, so the same charge generates
              automatically next period.
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
              just what is projected. The projection is shown
              alongside as informational context.
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
              dashboard's System health card. It reports the live
              status of the database and Redis, including latency.
              Failures there usually explain whatever the rest of the
              app is doing.
            </p>
          </section>

          <section>
            <h2 id="whats-next">What's next, known gaps</h2>
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

        <footer className="mt-12 border-t border-border pt-6 text-xs text-text-muted">
          See also:{" "}
          <Link href="/privacy" className="underline hover:text-text-primary">
            Privacy
          </Link>{" "}
          ·{" "}
          <Link href="/terms" className="underline hover:text-text-primary">
            Terms
          </Link>
        </footer>
      </article>
    </div>
  );
}
