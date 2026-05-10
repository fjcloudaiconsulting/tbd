import Link from "next/link";
import { btnPrimary } from "@/lib/styles";

export default function SecondCta() {
  return (
    <section className="mx-auto max-w-3xl px-6 py-16 text-center lg:py-20">
      <h2 className="font-display text-2xl font-semibold text-text-primary lg:text-3xl">
        Ready to see clearly?
      </h2>
      <p className="mx-auto mt-3 max-w-xl text-sm leading-relaxed text-text-secondary lg:text-base">
        No spreadsheets. No shame. Sign up free and start turning
        opacity into calm.
      </p>
      <Link
        href="/register"
        className={`${btnPrimary} mt-8 inline-block px-6 py-3`}
      >
        Get started free
      </Link>
    </section>
  );
}
