import Link from "next/link";
import type { Metadata } from "next";
import ThemeToggle from "@/components/ui/ThemeToggle";
import BackLink from "@/components/ui/BackLink";

export const metadata: Metadata = {
  title: "The Better Decision: Privacy Policy",
  description: "How The Better Decision collects, uses, and protects your personal data.",
};

const EFFECTIVE_DATE = "April 21, 2026";

export default function PrivacyPolicyPage() {
  return (
    <div className="relative min-h-screen px-4 py-12">
      <ThemeToggle className="absolute right-6 top-6" />
      <article className="mx-auto max-w-2xl">
        <header className="mb-10">
          <BackLink />
          <h1 className="mt-6 font-display text-3xl font-semibold text-text-primary">
            Privacy Policy
          </h1>
          <p className="mt-2 text-sm text-text-muted">
            Effective {EFFECTIVE_DATE}
          </p>
        </header>

        <div className="space-y-8 text-text-primary [&_h2]:font-display [&_h2]:text-xl [&_h2]:font-semibold [&_h2]:mb-3 [&_h2]:mt-8 [&_p]:text-sm [&_p]:leading-relaxed [&_p]:text-text-secondary [&_ul]:list-disc [&_ul]:pl-5 [&_ul]:text-sm [&_ul]:leading-relaxed [&_ul]:text-text-secondary [&_li]:mt-1">
          <section>
            <p>
              The Better Decision (&ldquo;we&rdquo;, &ldquo;us&rdquo;) is a personal finance
              application operated by Flamarion Jorge. We care about your
              privacy and keep what we collect to the minimum needed to run
              the service. This policy explains what we collect, why, how
              long we keep it, and your rights.
            </p>
          </section>

          <section>
            <h2>1. What we collect</h2>
            <p>When you create an account and use The Better Decision, we collect:</p>
            <ul>
              <li>
                <strong>Identity:</strong> username, email, first/last name,
                optional phone and avatar URL.
              </li>
              <li>
                <strong>Credentials:</strong> a bcrypt hash of your password
                (we never store the password itself). For Google sign-in, we
                store the verified email address Google provides. If you
                enable two-factor authentication, we store an encrypted TOTP
                secret and hashed recovery codes.
              </li>
              <li>
                <strong>Financial data you enter:</strong> accounts you
                create, transactions you record or import, budgets, forecasts,
                recurring items, and categories. This data stays scoped to
                your organization and is never shared with other
                organizations.
              </li>
              <li>
                <strong>Operational telemetry:</strong> request logs
                containing IP address, user agent, path, and timestamp for
                up to 30 days, used for debugging and abuse prevention.
              </li>
              <li>
                <strong>Cookies:</strong> an HTTP-only refresh-token cookie
                to keep you logged in, a theme preference in local storage,
                and a bot-management cookie set by Cloudflare. We do not use
                analytics or advertising cookies.
              </li>
            </ul>
          </section>

          <section>
            <h2>2. Why we collect it</h2>
            <ul>
              <li>
                To authenticate you and protect your account.
              </li>
              <li>
                To store and present the financial data you explicitly enter.
              </li>
              <li>
                To send transactional emails: account verification, password
                reset, MFA codes, and trial/billing notices.
              </li>
              <li>
                To debug errors, prevent abuse, and secure the service.
              </li>
            </ul>
            <p>
              We do not sell your data. We do not train AI models on your
              data. We do not use your data for advertising.
            </p>
          </section>

          <section>
            <h2>3. Third parties we share with</h2>
            <p>
              We share data only with service providers strictly necessary
              to run The Better Decision:
            </p>
            <ul>
              <li>
                <strong>DigitalOcean</strong> (hosting): stores your data at
                rest in managed MySQL in the ams3 region (EU).
              </li>
              <li>
                <strong>Cloudflare</strong> (CDN / edge): handles TLS
                termination, DDoS protection, and edge routing.
              </li>
              <li>
                <strong>Mailgun</strong> (EU region): sends transactional
                emails. We send only what&rsquo;s needed (your email
                address, the subject line, and the email body).
              </li>
              <li>
                <strong>Google</strong> (optional, if you choose
                &ldquo;Sign in with Google&rdquo;): we receive your email,
                name, and profile picture from Google after you authorize
                the sign-in.
              </li>
            </ul>
          </section>

          <section>
            <h2>4. How long we keep your data</h2>
            <ul>
              <li>
                Account and financial data are kept for the lifetime of your
                account.
              </li>
              <li>
                If you close your account, we delete your data within 30
                days. Backups are rotated within 90 days.
              </li>
              <li>
                Request logs are kept for up to 30 days.
              </li>
              <li>
                Email delivery records at Mailgun follow their retention
                policy (typically 3 days for logs).
              </li>
            </ul>
          </section>

          <section>
            <h2>5. Your rights (GDPR)</h2>
            <p>
              If you are in the EU/EEA, you have the following rights under
              the GDPR:
            </p>
            <ul>
              <li>
                <strong>Access</strong>: request a copy of your data.
              </li>
              <li>
                <strong>Rectification</strong>: fix anything inaccurate.
                Most of this is self-serve inside the app.
              </li>
              <li>
                <strong>Erasure</strong>: delete your account and all data
                associated with your organization.
              </li>
              <li>
                <strong>Portability</strong>: export your data in a
                machine-readable format.
              </li>
              <li>
                <strong>Restriction / Objection</strong>: ask us to stop
                or limit specific processing.
              </li>
              <li>
                <strong>Complaint</strong>: lodge a complaint with your
                national data-protection authority (in the Netherlands, the
                Autoriteit Persoonsgegevens).
              </li>
            </ul>
            <p>
              To exercise any of these rights, email us at{" "}
              <a
                href="mailto:privacy@thebetterdecision.com"
                className="underline hover:text-accent"
              >
                privacy@thebetterdecision.com
              </a>
              . We respond within 30 days.
            </p>
          </section>

          <section>
            <h2>6. Security</h2>
            <p>
              Passwords are stored as bcrypt hashes. TOTP secrets are
              encrypted at rest. All traffic is served over HTTPS with HSTS.
              Session refresh tokens are cookies scoped to the refresh
              endpoint with the HttpOnly, Secure, and SameSite=Lax flags.
              We review the application regularly with static analysis and
              third-party security tools.
            </p>
            <p>
              No system is perfectly secure. If you discover a vulnerability,
              please report it to{" "}
              <a
                href="mailto:security@thebetterdecision.com"
                className="underline hover:text-accent"
              >
                security@thebetterdecision.com
              </a>
              .
            </p>
          </section>

          <section>
            <h2>7. International transfers</h2>
            <p>
              Your data is stored in the EU (DigitalOcean ams3 region).
              Cloudflare may process requests at edge nodes globally as part
              of delivering the service. Mailgun operates in the EU region.
            </p>
          </section>

          <section>
            <h2>8. Children</h2>
            <p>
              The Better Decision is not intended for children under 16. We do not knowingly
              collect personal data from children.
            </p>
          </section>

          <section>
            <h2>9. Changes to this policy</h2>
            <p>
              When we make material changes we update the effective date and
              notify you by email before the changes take effect. The
              current version is always available at this page.
            </p>
          </section>

          <section>
            <h2>10. Contact</h2>
            <p>
              Privacy questions or requests:{" "}
              <a
                href="mailto:privacy@thebetterdecision.com"
                className="underline hover:text-accent"
              >
                privacy@thebetterdecision.com
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
          See also: <Link href="/terms" className="underline hover:text-text-primary">Terms of Service</Link>
        </footer>
      </article>
    </div>
  );
}
