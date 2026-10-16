import Link from "next/link";
import type { Metadata } from "next";
import ThemeToggle from "@/components/ui/ThemeToggle";
import BackLink from "@/components/ui/BackLink";
import CookiePreferencesButton from "@/components/landing/CookiePreferencesButton";

import { apexCanonical, pageSocialMeta, siteName } from "@/lib/site";
import { AI_TERMS_VERSION } from "@/lib/aiTerms";
import { PRIVACY_CONTACT_EMAIL } from "@/lib/dataPolicy";

const description =
  "The terms that apply when your organization turns on AI in The Better Decision.";

// Canonicalize to the apex host (served on both apex and app subdomain).
export const metadata: Metadata = {
  title: "AI Terms",
  description,
  alternates: {
    canonical: apexCanonical("/ai-terms"),
  },
  ...pageSocialMeta({
    title: `AI Terms · ${siteName}`,
    description,
    path: apexCanonical("/ai-terms"),
  }),
  robots: { index: true, follow: true },
};

const EFFECTIVE_DATE = "October 16, 2026";

export default function AiTermsPage() {
  return (
    <div className="relative min-h-screen px-4 py-12">
      <ThemeToggle className="absolute right-6 top-6" />
      <article className="mx-auto max-w-2xl">
        <header className="mb-10">
          <BackLink />
          <h1 className="mt-6 font-display text-3xl font-semibold text-text-primary">
            AI Terms
          </h1>
          <p className="mt-2 text-sm text-text-muted">
            Version {AI_TERMS_VERSION}
          </p>
          <p className="text-sm text-text-muted">
            Effective {EFFECTIVE_DATE}
          </p>
        </header>

        <div className="space-y-8 text-text-primary [&_h2]:font-display [&_h2]:text-xl [&_h2]:font-semibold [&_h2]:mb-3 [&_h2]:mt-8 [&_p]:text-sm [&_p]:leading-relaxed [&_p]:text-text-secondary [&_ul]:list-disc [&_ul]:pl-5 [&_ul]:text-sm [&_ul]:leading-relaxed [&_ul]:text-text-secondary [&_li]:mt-1">
          <section>
            <h2>1. What these terms cover</h2>
            <p>
              These terms apply when your organization turns on AI in The
              Better Decision. That covers the assistant, category
              suggestions, forecast refinement, budget rebalance, and AI apps
              you connect to your data. They add to our{" "}
              <Link href="/terms" className="underline hover:text-accent">Terms of Service</Link>{" "}
              and{" "}
              <Link href="/privacy" className="underline hover:text-accent">Privacy Policy</Link>.
            </p>
          </section>

          <section>
            <h2>2. AI is off until an admin turns it on</h2>
            <p>
              An admin of your organization turns AI on by accepting these
              terms in Settings, AI providers. Turning AI on starts no charges
              by itself. Any admin can turn AI off at any time. While AI is
              off, none of your organization&rsquo;s data is sent to any AI
              provider, and no connected AI app can read it.
            </p>
          </section>

          <section>
            <h2>3. Who processes the data, and where</h2>
            <p>
              The data stored in The Better Decision stays in the EU. What you
              send through an AI feature is the exception: it goes to an AI
              provider, which may process it outside the EU, including in the
              United States, under that provider&rsquo;s own terms.
            </p>
            <ul>
              <li>
                Your own provider: with your organization&rsquo;s own key, the
                provider it chose (OpenAI, Anthropic, an OpenAI-compatible
                service such as OpenRouter, or your own Ollama server)
                processes the data under your organization&rsquo;s agreement
                with it. You are responsible for that agreement and that key.
              </li>
              <li>
                Platform AI, where we supply the AI provider, is not offered
                yet. When we offer it, requests will go through OpenRouter,
                Inc. (United States), which passes each request to the company
                whose AI model answers it.
              </li>
              <li>
                Connected AI apps: an app you connect reads your data through
                our tools and sends it to the AI model it uses, under your
                agreement with the app&rsquo;s maker.
              </li>
            </ul>
          </section>

          <section>
            <h2>4. What is sent, and what is not</h2>
            <p>
              We never send your profile: your name, email address, phone
              number, username, or sign-in details. Before sending, we replace
              IBANs, card numbers, email addresses and phone numbers with
              placeholders. Names of people or businesses in descriptions, and
              what you type, are sent as written. Each feature sends only the
              data it needs; the{" "}
              <Link href="/privacy#ai-providers" className="underline hover:text-accent">Privacy Policy</Link>{" "}
              lists what that is.
            </p>
          </section>

          <section>
            <h2>5. AI output is a suggestion</h2>
            <p>
              AI can be wrong. Suggestions, forecasts and answers are not
              financial, tax, or legal advice. The assistant and connected
              apps cannot change your data on their own: every change is a
              preview that you confirm in the app. A preview you do not
              confirm within 10 minutes expires, and we delete each proposal
              record 48 hours after it was made.
            </p>
          </section>

          <section>
            <h2>6. Your part</h2>
            <p>
              Do not type anyone else&rsquo;s personal data into the assistant
              unless you need to. Keep your provider keys and agent tokens
              secret, and revoke a token you no longer use. Use AI only as our
              Terms of Service allow.
            </p>
          </section>

          <section>
            <h2>7. Spend</h2>
            <p>
              With your own key, your provider bills you. Spend caps in
              Settings, AI providers stop AI requests at the limit you set. We
              keep a usage record (feature, model, tokens, cost, time) for each
              request, never the text.
            </p>
          </section>

          <section>
            <h2>8. Changes to these terms</h2>
            <p>
              These terms have a version. When we make a material change, we
              publish a new version, and AI turns off for every organization
              until an admin reads and accepts it. Until then, nothing is sent
              to any AI provider and connected apps cannot read your data.
            </p>
          </section>

          <section>
            <h2>9. Contact</h2>
            <p>
              Questions:{" "}
              <a
                href={`mailto:${PRIVACY_CONTACT_EMAIL}`}
                className="underline hover:text-accent"
              >
                {PRIVACY_CONTACT_EMAIL}
              </a>
              .
            </p>
          </section>
        </div>

        <footer className="mt-12 flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-border pt-6 text-xs text-text-muted">
          <span>
            See also: <Link href="/terms" className="underline hover:text-text-primary">Terms of Service</Link>
            {" · "}
            <Link href="/privacy" className="underline hover:text-text-primary">Privacy Policy</Link>
          </span>
          <CookiePreferencesButton />
        </footer>
      </article>
    </div>
  );
}
