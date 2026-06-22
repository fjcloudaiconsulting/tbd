import Link from "next/link";
import { BRAND_NAME } from "@/lib/brand";
import { signinHref } from "@/lib/links";
import { btnPrimary, btnSecondary } from "@/lib/styles";
import HeroDashboard from "./HeroDashboard";
import SignupLink from "./SignupLink";

// Hero — spec §3.2 split layout. Left column carries the locked tagline
// (BRAND.md §Tagline), right column carries a stylized dashboard built
// from the same tokens as the real product. No em-dashes (locked policy
// `feedback_no_em_dashes`).
export default function Hero() {
  return (
    <section className="mx-auto max-w-6xl px-6 py-20 lg:px-10 lg:py-28">
      <div className="grid items-center gap-12 lg:grid-cols-2 lg:gap-16">
        <div className="motion-safe:animate-fade-in-up">
          <p className="mb-4 font-display text-xs font-semibold uppercase tracking-[0.18em] text-text-muted">
            {BRAND_NAME}
          </p>
          <h1 className="font-display font-semibold leading-[1.05] tracking-tight text-text-primary text-[clamp(2.5rem,5vw,4rem)]">
            There&rsquo;s no best decision.
            <br />
            Only better ones.
          </h1>
          <p className="mt-6 max-w-lg text-base leading-relaxed text-text-secondary lg:text-lg">
            See what is coming, not just what happened. {BRAND_NAME}{" "}
            forecasts your cash flow and plans your budget in one calm app,
            EU-hosted, for normal people.
          </p>
          <div className="mt-8 flex flex-wrap items-center gap-3">
            <SignupLink
              location="hero"
              className={`${btnPrimary} px-6 py-3 text-base`}
            >
              Get started free
            </SignupLink>
            <Link
              href={signinHref()}
              className={`${btnSecondary} px-6 py-3 text-base`}
            >
              Sign in
            </Link>
          </div>
          {/* Founding-members offer (2026-06-22). Copy only — a LIVE counter
              here can't ship to the apex static site (the apex bundle is
              forbidden from referencing the backend API; see
              frontend/scripts/build-apex.sh). The public count endpoint
              exists server-side for a future build-time or in-app counter.
              Regular hyphen (not an em-dash) per the customer-copy policy. */}
          <p className="mt-3 text-sm text-text-muted">
            Join as a founding member - free for life.
          </p>
          {/* Trust line under the CTAs. Three honest, verifiable claims;
              the dot separators match the footer convention. No fake
              urgency, no "limited time" framing (BRAND.md voice rules). */}
          <p className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-text-muted">
            <span>No card required</span>
            <span aria-hidden className="text-text-muted/60">&middot;</span>
            <span>EU-hosted</span>
            <span aria-hidden className="text-text-muted/60">&middot;</span>
            <span>Cancel anytime</span>
          </p>
        </div>
        <div className="motion-safe:animate-fade-in lg:pl-8">
          <HeroDashboard />
        </div>
      </div>
    </section>
  );
}
