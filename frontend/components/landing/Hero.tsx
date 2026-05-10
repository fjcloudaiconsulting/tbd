import Link from "next/link";
import { btnPrimary, btnSecondary } from "@/lib/styles";
import HeroDashboard from "./HeroDashboard";

export default function Hero() {
  return (
    <section className="mx-auto max-w-6xl px-6 py-16 lg:px-10 lg:py-24">
      <div className="grid items-center gap-12 lg:grid-cols-2 lg:gap-16">
        <div>
          <div className="mb-4 text-xs font-semibold uppercase tracking-[0.14em] text-text-muted">
            The Better Decision
          </div>
          <h1 className="font-display font-semibold leading-[1.05] text-text-primary text-[clamp(2.5rem,5vw,4rem)]">
            There&rsquo;s no best decision.
            <br />
            Only better ones.
          </h1>
          <p className="mt-6 max-w-lg text-base leading-relaxed text-text-secondary lg:text-lg">
            The Better Decision is a finance app for normal people. Know
            what you have, what&rsquo;s coming, and where it goes. No
            spreadsheet fatigue.
          </p>
          <div className="mt-8 flex flex-wrap items-center gap-3">
            <Link
              href="/register"
              className={`${btnPrimary} px-6 py-3`}
            >
              Get started free
            </Link>
            <Link
              href="/login"
              className={`${btnSecondary} px-6 py-3`}
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
