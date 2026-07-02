import Link from "next/link";
import type { Metadata } from "next";
import ThemeToggle from "@/components/ui/ThemeToggle";
import BackLink from "@/components/ui/BackLink";
import { readNonce } from "@/lib/nonce";
import { apexCanonical, apexUrl, pageSocialMeta, siteName } from "@/lib/site";

const description =
  "How to customize your dashboard: rearrange, resize, and add widgets, including charts cloned from your saved reports.";

// Canonicalize to the apex host (served on both apex and app subdomain).
export const metadata: Metadata = {
  title: "Dashboard guide",
  description,
  alternates: {
    canonical: apexCanonical("/docs/dashboard"),
  },
  ...pageSocialMeta({
    title: `Dashboard guide · ${siteName}`,
    description,
    path: apexCanonical("/docs/dashboard"),
  }),
  // The customizable dashboard (Feature.CUSTOM_DASHBOARD) now defaults ON in
  // prod, so this guide is generally available — indexable, matching the
  // /docs/plans precedent.
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
  headline: "Dashboard guide",
  description,
  url: apexCanonical("/docs/dashboard"),
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
      name: "Dashboard",
      item: apexCanonical("/docs/dashboard"),
    },
  ],
};

const structuredData = [techArticleLd, breadcrumbLd];

const sections = [
  { id: "overview", label: "What you can customize" },
  { id: "default-tiles", label: "The default tiles" },
  { id: "customize-mode", label: "Customize mode" },
  { id: "rearrange", label: "Rearrange & resize" },
  { id: "add-widgets", label: "Add widgets" },
  { id: "from-report", label: "Add a widget from a report" },
  { id: "recent-transactions", label: "Recent Transactions" },
  { id: "reset-save", label: "Reset & save" },
  { id: "mobile", label: "On mobile" },
  { id: "report-widgets", label: "Report widget types" },
];

export default async function DashboardDocsPage() {
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
            Docs · Dashboard
          </p>
          <h1 className="mt-2 font-display text-3xl font-semibold text-text-primary">
            Customizing your dashboard
          </h1>
          <p className="mt-2 text-sm text-text-muted">
            Your dashboard is a grid of widgets you can rearrange, resize,
            add to, and remove, including charts cloned from your saved
            reports. Your layout is saved to your account and is private to
            you.
          </p>
          <nav
            aria-label="On this page"
            className="mt-6 rounded-lg border border-border bg-surface p-4"
            data-testid="dashboard-docs-nav"
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
          <section data-testid="dashboard-docs-section-overview">
            <h2 id="overview">What you can customize</h2>
            <p>
              The dashboard is a 12-column grid of widgets. Every tile you
              see (your On-Track verdict, your accounts, your category
              charts, your recent transactions) is a widget you can move,
              resize, remove, or duplicate. You can also add new widgets,
              including any chart you have built in Reports.
            </p>
            <p>
              Two things stay fixed as chrome above the grid: the period
              navigator (the date-range stepper that drives what every tile
              shows) and the page title with the Customize button. Changing
              the period updates all the finance tiles at once.
            </p>
            <p>
              Nothing you do in Customize mode is saved until you press
              Save, so you are free to experiment.
            </p>
          </section>

          <section data-testid="dashboard-docs-section-default-tiles">
            <h2 id="default-tiles">The default tiles</h2>
            <p>
              A fresh dashboard starts with seven finance tiles. This is
              also what Reset to default restores:
            </p>
            <ul>
              <li>
                <strong>On Track.</strong> The verdict for the current
                period: planned spending, spent so far, and expected
                spending by period end.
              </li>
              <li>
                <strong>Accounts.</strong> Your accounts with their current
                balances and any pending amounts.
              </li>
              <li>
                <strong>Month-End Forecast.</strong> The projected balance
                for each account at the end of the period.
              </li>
              <li>
                <strong>Spending by Category.</strong> A donut of where this
                period's money went, with a sortable breakdown.
              </li>
              <li>
                <strong>Budget Progress.</strong> Spent versus budget per
                category for the period.
              </li>
              <li>
                <strong>Forecast by Category.</strong> Planned versus
                projected spend per category, colored under or over plan.
              </li>
              <li>
                <strong>Recent Transactions.</strong> A paginated table of
                this period's transactions. See its own section below.
              </li>
            </ul>
            <p>
              Remove any tile you do not want; it is never lost. You can
              re-add it from the widget picker at any time.
            </p>
          </section>

          <section data-testid="dashboard-docs-section-customize">
            <h2 id="customize-mode">Customize mode</h2>
            <p>
              Press <strong>Customize</strong> (top-right) to enter edit
              mode. Three more buttons appear (left to right:{" "}
              <strong>Save</strong>, <strong>Add widget</strong>, and{" "}
              <strong>Reset to default</strong>), and each tile gains a
              drag handle and a resize handle. Press <strong>Done</strong>{" "}
              to leave edit mode without saving, or Save to keep your
              changes.
            </p>
            <p>
              Customize mode is desktop-only. On a phone the dashboard is a
              read-only stack (see On mobile, below).
            </p>
          </section>

          <section data-testid="dashboard-docs-section-rearrange">
            <h2 id="rearrange">Rearrange &amp; resize</h2>
            <h3>Move a tile</h3>
            <p>
              Grab a tile by its drag handle (the grip icon, top-right of
              the tile) and drag it where you want it. As you drag over
              another tile, that tile moves aside to make room, and tiles
              float up to fill any gap, the same way app icons rearrange on
              a phone home screen. You cannot leave an empty gap above a
              tile; the grid always compacts to the top.
            </p>
            <h3>Resize a tile</h3>
            <p>
              Drag the resize handle at a tile's bottom-right corner. Tiles
              snap to the grid. A wider tile shows more of a chart; a taller
              Recent Transactions tile shows more rows.
            </p>
          </section>

          <section data-testid="dashboard-docs-section-add">
            <h2 id="add-widgets">Add widgets</h2>
            <p>
              In Customize mode, press <strong>Add widget</strong>. The
              picker has two groups:
            </p>
            <ul>
              <li>
                <strong>Dashboard tiles.</strong> Re-add any of the seven
                built-in finance tiles. You can add a tile more than once.
              </li>
              <li>
                <strong>From a report.</strong> Clone a chart you built in
                Reports onto the dashboard; see the next section.
              </li>
            </ul>
            <p>
              A new widget lands at the bottom of the grid; drag it into
              place from there.
            </p>
          </section>

          <section data-testid="dashboard-docs-section-from-report">
            <h2 id="from-report">Add a widget from a report</h2>
            <p>
              Any widget you have saved in a report can live on your
              dashboard. In the Add widget picker, choose <strong>From a
              report</strong>, pick one of your saved reports, then pick one
              of its widgets. It is copied onto your dashboard.
            </p>
            <p>
              The copy is independent: it keeps its own configuration (data
              source, measure, filters, chart type) and queries your live
              data on its own. Editing or deleting the original report does
              not change the dashboard copy, and vice versa. Every report
              widget type can be cloned, including the cash-flow Sankey.
            </p>
            <p>
              This is the quickest way to pin a favorite analysis (a
              spending trend, a category breakdown, your cash-flow Sankey)
              next to your everyday finance tiles.
            </p>
          </section>

          <section data-testid="dashboard-docs-section-recent-tx">
            <h2 id="recent-transactions">Recent Transactions</h2>
            <p>
              The Recent Transactions tile is a table of this period's
              transactions, newest first. A few things are worth knowing:
            </p>
            <ul>
              <li>
                <strong>Rows per page.</strong> Use the selector in the
                tile's footer to show 10, 25, 50, or 100 rows. Raise it to
                fill a taller tile instead of leaving blank space.
              </li>
              <li>
                <strong>Scrolls to fit.</strong> If the tile is shorter than
                the rows it holds, the list scrolls inside the card; the
                header and the pager stay pinned.
              </li>
              <li>
                <strong>Toggle status inline.</strong> Click a row's status
                pill to flip it between settled and pending. The charts and
                balances refresh to match.
              </li>
              <li>
                <strong>Settled date.</strong> Each row shows the
                transaction date and, beneath it, the settled date (an
                em-dash until it settles), because we count a transaction in
                the period it actually clears.
              </li>
              <li>
                <strong>Sort.</strong> Click a column header (Date,
                Description, Status, Amount) to sort by it.
              </li>
            </ul>
            <p>
              Your rows-per-page choice applies for the current session. The
              tile resets to its default the next time you load the page.
            </p>
          </section>

          <section data-testid="dashboard-docs-section-reset-save">
            <h2 id="reset-save">Reset &amp; save</h2>
            <h3>Reset to default</h3>
            <p>
              <strong>Reset to default</strong> (in Customize mode) restores
              the seven-tile starting layout. It asks for confirmation
              first, and, like every change in Customize mode, it does not
              persist until you press Save, so you can preview the default
              and back out.
            </p>
            <h3>Save</h3>
            <p>
              Saving is explicit: press <strong>Save</strong> to store your
              layout. It is saved to your account and is private to you, so
              it follows you across devices and survives a reload. A "Saved"
              note confirms the write. Leaving Customize mode with Done
              discards unsaved changes.
            </p>
          </section>

          <section data-testid="dashboard-docs-section-mobile">
            <h2 id="mobile">On mobile</h2>
            <p>
              On a phone the dashboard renders as a single, read-only column:
              every tile stacked top to bottom in your saved order. Editing
              (drag, resize, add, remove) is desktop-only; switch to a larger
              screen to rearrange.
            </p>
          </section>

          <section data-testid="dashboard-docs-section-report-widgets">
            <h2 id="report-widgets">Report widget types</h2>
            <p>
              Reports is where you build the charts you can clone onto your
              dashboard. A report is a canvas of widgets; each widget has a
              data source (your transactions or your accounts), a measure
              (sum, average, or count), one or more dimensions (category,
              account, month, status, and so on), and optional filters.
              These widget types are available:
            </p>
            <ul>
              <li>
                <strong>KPI.</strong> A single headline number (for example,
                total spend this period).
              </li>
              <li>
                <strong>Bar</strong> and <strong>Stacked bar.</strong>{" "}
                Compare a measure across a dimension, optionally split by a
                second dimension.
              </li>
              <li>
                <strong>Line</strong> and <strong>Area.</strong> A measure
                over time.
              </li>
              <li>
                <strong>Pie / Donut.</strong> A measure's share across a
                category.
              </li>
              <li>
                <strong>Sparkline.</strong> A compact trend with no axes.
              </li>
              <li>
                <strong>Table.</strong> The underlying rows, sortable.
              </li>
              <li>
                <strong>Sankey (cash flow).</strong> Money flowing from
                income sources through to spending categories and savings:
                the signature cash-flow view.
              </li>
            </ul>
            <p>
              Build a widget once in a report, then clone it onto your
              dashboard from the Add widget picker. The dashboard reuses the
              same grid, so rearranging and resizing work identically on both
              surfaces.
            </p>
          </section>
        </div>

        <footer className="mt-12 border-t border-border pt-6 text-xs text-text-muted">
          See also:{" "}
          <Link href="/docs" className="underline hover:text-text-primary">
            Docs home
          </Link>{" "}
          ·{" "}
          <Link href="/dashboard" className="underline hover:text-text-primary">
            Dashboard
          </Link>{" "}
          ·{" "}
          <Link href="/reports" className="underline hover:text-text-primary">
            Reports
          </Link>
        </footer>
      </article>
    </div>
  );
}
