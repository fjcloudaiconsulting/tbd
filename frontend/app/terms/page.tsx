import Link from "next/link";
import type { Metadata } from "next";
import ThemeToggle from "@/components/ui/ThemeToggle";

export const metadata: Metadata = {
  title: "Terms of Service — PFV2",
  description: "The agreement between you and PFV2 when you use the service.",
};

const EFFECTIVE_DATE = "April 21, 2026";

export default function TermsOfServicePage() {
  return (
    <div className="relative min-h-screen px-4 py-12">
      <ThemeToggle className="absolute right-6 top-6" />
      <article className="mx-auto max-w-2xl">
        <header className="mb-10">
          <Link
            href="/login"
            className="text-sm text-text-muted hover:text-text-primary"
          >
            ← Back
          </Link>
          <h1 className="mt-6 font-display text-3xl font-semibold text-text-primary">
            Terms of Service
          </h1>
          <p className="mt-2 text-sm text-text-muted">
            Effective {EFFECTIVE_DATE}
          </p>
        </header>

        <div className="space-y-8 text-text-primary [&_h2]:font-display [&_h2]:text-xl [&_h2]:font-semibold [&_h2]:mb-3 [&_h2]:mt-8 [&_p]:text-sm [&_p]:leading-relaxed [&_p]:text-text-secondary [&_ul]:list-disc [&_ul]:pl-5 [&_ul]:text-sm [&_ul]:leading-relaxed [&_ul]:text-text-secondary [&_li]:mt-1">
          <section>
            <p>
              Welcome to PFV2. These Terms of Service (&ldquo;Terms&rdquo;)
              govern your use of the PFV2 personal finance application
              operated by Flamarion Jorge. By creating an account or using
              the service you agree to these Terms.
            </p>
          </section>

          <section>
            <h2>1. The service</h2>
            <p>
              PFV2 lets you track personal or small-organization finances:
              accounts, transactions, budgets, forecasts, recurring items,
              and related tooling. It&rsquo;s a bookkeeping and planning
              tool, not a bank, broker, or financial advisor.
            </p>
            <p>
              <strong>Beta notice.</strong> PFV2 is in an early stage.
              Features may change, data models may evolve, and we may need
              to reset or migrate parts of the service. We will give
              reasonable notice and make best-effort data preservation
              decisions, but we cannot offer a formal SLA during beta.
            </p>
          </section>

          <section>
            <h2>2. Your account</h2>
            <ul>
              <li>
                Provide accurate information when you sign up and keep it
                up to date.
              </li>
              <li>
                Keep your password and MFA recovery codes safe. You are
                responsible for activity on your account.
              </li>
              <li>
                One account per individual. You may operate multiple
                organizations from a single account.
              </li>
              <li>
                You must be at least 16 years old to create an account.
              </li>
            </ul>
          </section>

          <section>
            <h2>3. Acceptable use</h2>
            <p>You agree not to:</p>
            <ul>
              <li>
                Attempt to access other users&rsquo; data or organizations.
              </li>
              <li>
                Reverse-engineer, scrape, or automate the service beyond
                reasonable personal use, or in a way that imposes
                disproportionate load.
              </li>
              <li>
                Upload or store unlawful content, or use the service for
                money laundering, fraud, or evasion of tax or sanctions
                obligations.
              </li>
              <li>
                Probe, scan, or test the vulnerability of the service
                without prior written permission from us.
              </li>
            </ul>
            <p>
              We may suspend or close accounts that violate these rules.
            </p>
          </section>

          <section>
            <h2>4. Your data and content</h2>
            <p>
              You own the financial data you enter. We process it on your
              behalf as described in the{" "}
              <Link href="/privacy" className="underline hover:text-accent">
                Privacy Policy
              </Link>
              . You can export or delete your data at any time by closing
              your account; within 30 days of closure we delete your data
              from production systems.
            </p>
          </section>

          <section>
            <h2>5. Subscriptions and billing</h2>
            <p>
              Paid plans are billed in advance at the stated price. During
              the current beta period, billing flows display a clear
              &ldquo;mock / no charge&rdquo; notice and no money changes
              hands; when we switch to real payments we will notify every
              user by email before charging begins and honor any free-trial
              periods already granted.
            </p>
            <p>
              You can cancel at any time. Canceling stops future renewals;
              the current period remains active until its end. We do not
              prorate or refund partial periods unless required by law.
            </p>
          </section>

          <section>
            <h2>6. Not financial advice</h2>
            <p>
              PFV2 displays numbers, forecasts, and categorizations based on
              the data you enter. Forecasts are estimates, not predictions.
              Nothing in the app is investment advice, legal advice, tax
              advice, or a substitute for a qualified professional.
              Decisions you make based on the app are your own.
            </p>
          </section>

          <section>
            <h2>7. Third parties and integrations</h2>
            <p>
              PFV2 integrates with third-party services (Google for sign-in,
              Mailgun for email, Cloudflare for edge, DigitalOcean for
              hosting). Your use of those integrations is additionally
              governed by the third party&rsquo;s terms. We are not
              responsible for third-party outages or data handling beyond
              what&rsquo;s described in our Privacy Policy.
            </p>
          </section>

          <section>
            <h2>8. Intellectual property</h2>
            <p>
              The PFV2 application, including its code, design, and
              trademarks, belongs to us. These Terms do not grant you any
              rights beyond the license to use the service for its intended
              purpose.
            </p>
          </section>

          <section>
            <h2>9. Warranty disclaimer</h2>
            <p>
              The service is provided &ldquo;as is&rdquo; and &ldquo;as
              available&rdquo;. We make no warranty of merchantability,
              fitness for a particular purpose, uptime, data accuracy, or
              non-infringement, except as required by mandatory applicable
              law.
            </p>
          </section>

          <section>
            <h2>10. Limitation of liability</h2>
            <p>
              To the maximum extent permitted by law, we are not liable for
              indirect, incidental, consequential, or special damages,
              including lost profits or lost data. Our total liability for
              any claim related to the service is limited to the greater of
              (a) the amount you paid us in the 12 months before the claim
              arose, or (b) &euro;100.
            </p>
            <p>
              Nothing in these Terms limits liability that cannot be limited
              under applicable law (for example, gross negligence,
              intentional misconduct, or statutory consumer rights).
            </p>
          </section>

          <section>
            <h2>11. Termination</h2>
            <p>
              You may close your account at any time. We may suspend or
              close accounts that violate these Terms, fail to pay, or pose
              a security risk. We&rsquo;ll give you reasonable notice when
              practical.
            </p>
          </section>

          <section>
            <h2>12. Changes to these Terms</h2>
            <p>
              When we make material changes we update the effective date and
              notify you by email. Continued use after the effective date
              means you accept the updated Terms.
            </p>
          </section>

          <section>
            <h2>13. Governing law</h2>
            <p>
              These Terms are governed by the laws of the Netherlands,
              without regard to conflict-of-law rules. Disputes are subject
              to the exclusive jurisdiction of the competent courts in the
              Netherlands, except where mandatory consumer-protection law
              gives you rights to bring a claim in your country of
              residence.
            </p>
          </section>

          <section>
            <h2>14. Contact</h2>
            <p>
              Questions about these Terms:{" "}
              <a
                href="mailto:legal@thebetterdecision.com"
                className="underline hover:text-accent"
              >
                legal@thebetterdecision.com
              </a>
              . General contact:{" "}
              <a
                href="mailto:hello@thebetterdecision.com"
                className="underline hover:text-accent"
              >
                hello@thebetterdecision.com
              </a>
              .
            </p>
          </section>
        </div>

        <footer className="mt-12 border-t border-border pt-6 text-xs text-text-muted">
          See also: <Link href="/privacy" className="underline hover:text-text-primary">Privacy Policy</Link>
        </footer>
      </article>
    </div>
  );
}
