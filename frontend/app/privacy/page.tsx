import Link from "next/link";
import type { Metadata } from "next";
import ThemeToggle from "@/components/ui/ThemeToggle";
import BackLink from "@/components/ui/BackLink";
import CookiePreferencesButton from "@/components/landing/CookiePreferencesButton";

import { apexCanonical, pageSocialMeta, siteName } from "@/lib/site";
import {
  BACKUP_ROTATION_WINDOW_DAYS,
  DATA_DELETION_WINDOW_DAYS,
  PRIVACY_CONTACT_EMAIL,
  REQUEST_LOG_RETENTION_DAYS,
} from "@/lib/dataPolicy";

const description =
  "How The Better Decision collects, uses, and protects your personal data.";

// Canonicalize to the apex host. This page is served on both the apex and
// the app subdomain; pointing both copies' canonical at the apex keeps the
// ranking signal on one URL instead of splitting it across hosts.
export const metadata: Metadata = {
  title: "Privacy Policy",
  description,
  alternates: {
    canonical: apexCanonical("/privacy"),
  },
  ...pageSocialMeta({
    title: `Privacy Policy · ${siteName}`,
    description,
    path: apexCanonical("/privacy"),
  }),
  robots: { index: true, follow: true },
};

const EFFECTIVE_DATE = "October 5, 2026";
const LAST_UPDATED = "October 25, 2026";

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
          <p className="text-sm text-text-muted">
            Last updated {LAST_UPDATED}
          </p>
        </header>

        <div className="space-y-8 text-text-primary [&_h2]:font-display [&_h2]:text-xl [&_h2]:font-semibold [&_h2]:mb-3 [&_h2]:mt-8 [&_p]:text-sm [&_p]:leading-relaxed [&_p]:text-text-secondary [&_ul]:list-disc [&_ul]:pl-5 [&_ul]:text-sm [&_ul]:leading-relaxed [&_ul]:text-text-secondary [&_li]:mt-1">
          <section>
            <p>
              The Better Decision (&ldquo;we&rdquo;, &ldquo;us&rdquo;) is a personal finance
              application operated by FJ Cloud &amp; AI Consulting. We care about your
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
                up to {REQUEST_LOG_RETENTION_DAYS} days, used for debugging and abuse prevention.
                Performance metrics and traces without personal data go to
                our monitoring provider (section 3).
              </li>
              <li>
                <strong>Cookies:</strong> an HTTP-only refresh-token cookie
                to keep you logged in, a short-lived <code>oauth_state</code>{" "}
                cookie issued during Google sign-in (deleted within 10 minutes
                of the sign-in flow completing or being cancelled), a theme
                preference in local storage, the current assistant
                conversation in the tab&rsquo;s session storage, and a
                bot-management cookie set
                by Cloudflare. On our public marketing pages (never once you
                are signed in to the app), we use Google Analytics 4. It runs
                only after you opt in: a consent banner lets you accept or
                reject, and until you accept, Google Analytics is loaded in a
                consent-denied mode (Google Consent Mode v2) that sets no{" "}
                <code>_ga</code> / <code>_ga_*</code> cookies. If you accept,
                those first-party cookies measure aggregate, anonymized visitor
                traffic; we do not use this data for advertising. Your choice is
                stored in a <code>tbd-consent-v1</code> local-storage entry and
                re-requested every six months. You can change or withdraw it
                anytime via the &ldquo;Cookie preferences&rdquo; link in the
                page footer.
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
                To run the AI features your organization turns on (section 4).
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
                <strong>Hetzner</strong> (hosting): runs the service and
                stores your data at rest, including the database, on servers
                in Nuremberg (Germany, EU).
              </li>
              <li>
                <strong>Amazon Web Services</strong> (backups): stores our
                nightly encrypted database backups in the Frankfurt region
                (eu-central-1, Germany, EU).
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
              <li>
                <strong>Google</strong> (Analytics, on our public marketing
                pages only, and only if you accept analytics cookies):
                Google Analytics 4 receives aggregate, anonymized visitor
                traffic data, as described in section 1.
              </li>
              <li>
                <strong>AI providers</strong> (only while AI is on for your
                organization): the provider your organization sets up with its
                own key, under your organization&rsquo;s agreement with it, and
                platform AI once we offer it. Section 4 explains what is sent
                and where it is processed.
              </li>
              <li>
                <strong>Grafana Labs</strong> (Grafana Cloud, EU region,
                monitoring): receives operational metrics and traces, made of
                the request method, route pattern, status code, error type,
                timing, scheduled job names, and database query text without
                its values. It never receives
                your financial data, request contents, IP address, or request
                logs.
              </li>
            </ul>
          </section>

          <section id="ai-providers">
            <h2>4. AI features</h2>
            <p>
              <strong>AI is off until you turn it on.</strong> An admin of
              your organization turns AI on in Settings, AI providers, after
              accepting our <Link href="/ai-terms" className="underline hover:text-accent">AI Terms</Link>.
              An admin can turn it off at any time.
            </p>
            <p>
              <strong>Where AI data is processed.</strong> The data stored in
              The Better Decision stays in the EU (section 8). What you send
              through an AI feature is the exception: it goes to an AI
              provider, which may process it outside the EU, including in the
              United States, under that provider&rsquo;s own terms.
            </p>
            <ul>
              <li>
                With your organization&rsquo;s own key, the provider is the one
                it chose: OpenAI, Anthropic, or an OpenAI-compatible service
                such as OpenRouter. Your organization&rsquo;s agreement with
                that provider applies.
              </li>
              <li>
                Platform AI, where we supply the AI provider, is not offered
                yet. When we offer it, requests will go through OpenRouter,
                Inc. (United States), which passes each request to the company
                whose AI model answers it.
              </li>
              <li>
                A connected AI app sends the data it reads to the AI model it
                uses, wherever its maker processes it.
              </li>
            </ul>
            <p>
              <strong>What we never send.</strong> We never send your profile
              to an AI provider: your name, email address, phone number,
              username, and sign-in details.
            </p>
            <p>
              <strong>What we remove first.</strong> Before any text leaves our
              servers for an AI provider or a connected AI app, we replace
              the IBANs, card numbers, email addresses and phone numbers that
              we recognize with placeholders such as [IBAN] and [CARD]. An
              unusual format can slip through.
            </p>
            <p>
              <strong>What is sent as written.</strong> Names of people or
              businesses in your transaction descriptions, account and
              category names, and everything else you type to the assistant
              are sent as you wrote them. If you do not want a name to reach
              an AI provider, leave it out of what you type.
            </p>
            <p><strong>What each feature sends:</strong></p>
            <ul>
              <li>
                Assistant: what you type, and the data it looks up to answer
                you. That can be account names and types, balances
                (including opening balances) and currencies, credit limits, interest rates, payment days, and loan
                amounts, rates and terms; categories; budgets; transactions
                (description, amount, date, status, category, account and
                tags); and spending and forecast figures.
              </li>
              <li>
                Suggest a category: one transaction&rsquo;s description, amount
                and type, plus your category names and descriptions.
              </li>
              <li>
                Forecast refinement: monthly spending totals per category,
                category names, and the forecast figures. No transaction
                descriptions.
              </li>
              <li>
                Budget rebalance: budget amounts, spending and projections for
                each category, plus category names.
              </li>
              <li>
                Plans smart suggestions, when available: the plan type and
                length, the plan&rsquo;s numeric assumptions, and the average
                monthly net of each account, without account names.
              </li>
            </ul>
            <p>
              <strong>Assistant conversations.</strong> What you send to the
              assistant goes to your organization&rsquo;s AI provider, which is
              a cloud service unless your organization runs its own AI
              server. We do not store your conversations with the assistant on our
              servers. A conversation is kept in your browser tab until you
              sign out or start a new chat, and it is not restored once 2
              hours have passed since your last message. A browser that
              reopens closed tabs can bring it back within that time.
            </p>
            <p>
              <strong>Changes the assistant proposes.</strong> The
              assistant stages each change as a proposal that you confirm in
              the app. A connected AI app with read-only access only reads.
              With change access, its changes are staged as proposals that
              only that AI app can confirm, using the same token. A token you
              create with automatic access applies changes without a
              proposal; apps connected through sign-in never get automatic
              access. A proposal you do not
              confirm within 10 minutes expires. We delete each proposal
              record (what was asked, the preview, and what happened) 48 hours
              after the proposal was made. When a proposal is carried out, our
              security log keeps a short entry: the kind of change, when it
              happened, and whether it succeeded, without its details.
            </p>
            <p>
              <strong>Connected AI apps.</strong> You can connect an AI app,
              such as Claude Desktop or another MCP client, with an agent
              token or by approving it at sign-in. That app reads your data
              through our tools, and we remove the identifiers listed above
              first. The app then sends that data to the AI model it uses,
              under your agreement with the app&rsquo;s maker. We do not
              control that step. You can revoke an app&rsquo;s access in
              Settings, Agent access, and an org admin can revoke any
              member&rsquo;s token.
            </p>
            <p>
              <strong>When AI is off.</strong> When AI is off, none of your
              data is sent to any AI provider. No connected AI app can read
              your data either: its requests are refused, and it cannot get
              new access. If an admin turns AI back on, apps that were already
              connected work again. While AI is on but no AI provider is set
              up, AI features send nothing, though a connected AI app can
              still read your data.
            </p>
            <p>
              <strong>Usage records.</strong> For each AI request we keep the
              feature, model, token counts, cost and time, so we can apply
              spend caps. We never keep the text.
            </p>
          </section>

          <section>
            <h2>5. How long we keep your data</h2>
            <ul>
              <li>
                Account and financial data are kept for the lifetime of your
                account.
              </li>
              <li>
                If you close your account, we delete your data within{" "}
                {DATA_DELETION_WINDOW_DAYS} days. Backups are rotated within{" "}
                {BACKUP_ROTATION_WINDOW_DAYS} days.
              </li>
              <li>
                Request logs are kept for up to {REQUEST_LOG_RETENTION_DAYS} days.
                Backups of our servers that may contain these logs are
                deleted within 14 days.
              </li>
              <li>
                Email delivery records at Mailgun follow their retention
                policy (typically 3 days for logs).
              </li>
              <li>
                Metrics and traces at Grafana Cloud follow its retention
                policy.
              </li>
              <li>
                Records of changes proposed by the assistant or a connected AI
                app are deleted 48 hours after the proposal was made.
              </li>
              <li>
                We do not store assistant conversations.
              </li>
              <li>
                AI usage records (feature, model, token counts, cost, time;
                never the text) are kept for the lifetime of your account.
              </li>
              <li>
                What an AI provider keeps follows that provider&rsquo;s terms.
                With your own key, your organization&rsquo;s agreement with the
                provider applies.
              </li>
            </ul>
          </section>

          <section>
            <h2>6. Your rights (GDPR)</h2>
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
                {PRIVACY_CONTACT_EMAIL}
              </a>
              . We respond within 30 days.
            </p>
          </section>

          <section>
            <h2>7. Security</h2>
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
            <h2>8. International transfers</h2>
            <p>
              Your data is stored in the EU: the service runs at Hetzner in
              Nuremberg, Germany, and our backups are kept at Amazon Web
              Services in the Frankfurt region, Germany. Grafana Cloud stores
              operational metrics and traces in its EU region.
              Cloudflare may process requests at edge nodes globally as part
              of delivering the service. Mailgun operates in the EU region.
            </p>
            <p>
              AI features are the exception. While AI is on, what you send
              through an AI feature goes to an AI provider that may process it
              outside the EU, including in the United States, under that
              provider&rsquo;s own terms. Section 4 explains which provider
              that is.
            </p>
          </section>

          <section>
            <h2>9. Children</h2>
            <p>
              The Better Decision is not intended for children under 16. We do not knowingly
              collect personal data from children.
            </p>
          </section>

          <section>
            <h2>10. Changes to this policy</h2>
            <p>
              When we make material changes we update the effective date and
              notify you by email before the changes take effect. The
              current version is always available at this page.
            </p>
          </section>

          <section>
            <h2>11. Contact</h2>
            <p>
              Privacy questions or requests:{" "}
              <a
                href="mailto:privacy@thebetterdecision.com"
                className="underline hover:text-accent"
              >
                {PRIVACY_CONTACT_EMAIL}
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

        <footer className="mt-12 flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-border pt-6 text-xs text-text-muted">
          <span>
            See also: <Link href="/terms" className="underline hover:text-text-primary">Terms of Service</Link>
            {" · "}
            <Link href="/ai-terms" className="underline hover:text-text-primary">AI Terms</Link>
          </span>
          <CookiePreferencesButton />
        </footer>
      </article>
    </div>
  );
}
