import Link from "next/link";
import { Logo, Mark } from "@/components/brand/Logo";
import ThemeToggle from "@/components/ui/ThemeToggle";
import { signinHref, signupHref } from "@/lib/links";
import { btnPrimary } from "@/lib/styles";

// Public landing nav per spec §3.1: brand lockup on the left, Sign in
// + Get started + theme toggle on the right. Uses the canonical <Logo />
// from PR #224 so the brand name never gets re-typed inline.
export default function TopNav() {
  return (
    <nav
      aria-label="Primary"
      className="mx-auto flex max-w-6xl items-center justify-between px-6 py-5 lg:px-10"
    >
      <Link
        href="/"
        className="rounded-sm hover:opacity-80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
        aria-label="The Better Decision, home"
      >
        {/* sm:hidden compact mark on phones, full lockup from sm up so
            the wordmark never wraps next to crowded right-side actions. */}
        <span className="sm:hidden">
          <Mark size="md" />
        </span>
        <span className="hidden sm:inline-flex">
          <Logo size="md" />
        </span>
      </Link>
      <div className="flex items-center gap-2 sm:gap-4">
        {/* In-page jump link to the FAQ section. Hidden on phones to
            keep the right-hand actions clear of crowding. The anchor
            target (#faq) is a stable ID on the FAQ section so
            deep-links from emails or future blog posts land directly. */}
        <a
          href="#faq"
          className="hidden whitespace-nowrap px-2 text-sm text-text-muted transition-colors hover:text-text-primary md:inline"
        >
          FAQ
        </a>
        <Link
          href={signinHref()}
          className="whitespace-nowrap px-2 text-sm text-text-muted transition-colors hover:text-text-primary"
        >
          Sign in
        </Link>
        <Link
          href={signupHref()}
          className={`${btnPrimary} whitespace-nowrap`}
        >
          Get started
        </Link>
        <ThemeToggle />
      </div>
    </nav>
  );
}
