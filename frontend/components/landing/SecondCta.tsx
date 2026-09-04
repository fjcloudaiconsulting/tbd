import type { SignupCtaLocation } from "@/lib/analytics";
import { btnPrimary } from "@/lib/styles";
import SignupLink from "./SignupLink";

// Spec §3.4 — centered block, single primary CTA. The heading is the
// one-liner above the button per the spec; the subline is voice-grade
// brand copy (docs/product/BRAND.md voice section: honest, brief, no fake urgency).
//
// `location` is the GA4 `cta_location` this block reports. It DEFAULTS to
// "second_cta" so the homepage's long-standing telemetry is byte-identical;
// secondary marketing pages that reuse this block pass their own distinct
// value so the conversion breakdown can attribute the click to a page.
export default function SecondCta({
  location = "second_cta",
}: {
  location?: SignupCtaLocation;
}) {
  return (
    <section className="mx-auto max-w-3xl px-6 py-20 text-center lg:py-24">
      <h2 className="font-display text-3xl font-semibold leading-tight text-text-primary lg:text-4xl">
        Ready to see clearly?
      </h2>
      <p className="mx-auto mt-4 max-w-xl text-sm leading-relaxed text-text-secondary lg:text-base">
        No spreadsheets, no shame. Sign up free and start turning opacity
        into calm.
      </p>
      <SignupLink
        location={location}
        className={`${btnPrimary} mt-8 inline-block px-6 py-3 text-base`}
      >
        Get started free
      </SignupLink>
    </section>
  );
}
