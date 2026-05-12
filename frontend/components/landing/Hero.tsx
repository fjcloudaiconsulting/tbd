import Link from "next/link";
import { BRAND_NAME } from "@/lib/brand";
import { btnPrimary, btnSecondary } from "@/lib/styles";
import HeroDashboard from "./HeroDashboard";

// Hero — spec §3.2 split layout. Left column carries the locked tagline
// (BRAND.md §Tagline), right column carries a stylized dashboard built
// from the same tokens as the real product. No em-dashes (locked policy
// `feedback_no_em_dashes`).
export default function Hero() {
  return (
    <section className="mx-auto max-w-6xl px-6 py-20 lg:px-10 lg:py-28">
      <div className="grid items-center gap-12 lg:grid-cols-2 lg:gap-16">
        <div>
          <p className="mb-4 font-display text-xs font-semibold uppercase tracking-[0.18em] text-text-muted">
            {BRAND_NAME}
          </p>
          <h1 className="font-display font-semibold leading-[1.05] tracking-tight text-text-primary text-[clamp(2.5rem,5vw,4rem)]">
            There&rsquo;s no best decision.
            <br />
            Only better ones.
          </h1>
          <p className="mt-6 max-w-lg text-base leading-relaxed text-text-secondary lg:text-lg">
            {BRAND_NAME}
            {" "}
            is a finance app for normal people. Know what you have,
            what&rsquo;s coming, and where it goes, without the
            spreadsheet fatigue.
          </p>
          <div className="mt-8 flex flex-wrap items-center gap-3">
            <Link
              href="/register"
              className={`${btnPrimary} px-6 py-3 text-base`}
            >
              Get started free
            </Link>
            <Link
              href="/login"
              className={`${btnSecondary} px-6 py-3 text-base`}
            >
              Sign in
            </Link>
          </div>
        </div>
        <div className="lg:pl-8">
          <HeroDashboard />
        </div>
      </div>
    </section>
  );
}
