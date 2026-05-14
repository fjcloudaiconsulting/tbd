import Link from "next/link";
import { signupHref } from "@/lib/links";
import { btnPrimary } from "@/lib/styles";

// Spec §3.4 — centered block, single primary CTA. The heading is the
// one-liner above the button per the spec; the subline is voice-grade
// brand copy (BRAND.md voice section: honest, brief, no fake urgency).
export default function SecondCta() {
  return (
    <section className="mx-auto max-w-3xl px-6 py-20 text-center lg:py-24">
      <h2 className="font-display text-3xl font-semibold leading-tight text-text-primary lg:text-4xl">
        Ready to see clearly?
      </h2>
      <p className="mx-auto mt-4 max-w-xl text-sm leading-relaxed text-text-secondary lg:text-base">
        No spreadsheets, no shame. Sign up free and start turning opacity
        into calm.
      </p>
      <Link
        href={signupHref()}
        className={`${btnPrimary} mt-8 inline-block px-6 py-3 text-base`}
      >
        Get started free
      </Link>
    </section>
  );
}
