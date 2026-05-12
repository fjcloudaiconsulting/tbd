import Link from "next/link";
import { Logo } from "@/components/brand/Logo";
import CurrentYear from "@/components/ui/CurrentYear";
import { BRAND_CONTACT_EMAIL } from "@/lib/brand";

// Landing footer per spec §3.5: muted wordmark + copyright on the left,
// Privacy / Terms / Help / contact on the right. The Help label routes
// to /docs (the existing in-app user manual from PR #159) because there
// is no /help route, and a public 404 is not acceptable for launch.
export default function LandingFooter() {
  return (
    <footer className="border-t border-border">
      <div className="mx-auto flex max-w-6xl flex-col gap-4 px-6 py-10 text-xs text-text-muted lg:flex-row lg:items-center lg:justify-between lg:px-10">
        <div className="flex items-center gap-3">
          <Logo tone="muted" size="sm" />
          <span className="inline-flex items-center gap-1">
            <span aria-hidden>&copy;</span>
            <CurrentYear />
          </span>
        </div>
        <nav
          aria-label="Footer"
          className="flex flex-wrap items-center gap-5"
        >
          <Link href="/privacy" className="hover:text-text-primary">
            Privacy
          </Link>
          <Link href="/terms" className="hover:text-text-primary">
            Terms
          </Link>
          {/* /docs is the public in-app user manual (PR #159). The spec's
              "/help" placeholder is unbuilt; routing Help to /docs keeps
              the link live for launch and reuses the manual until a
              dedicated /help support page lands. */}
          <Link href="/docs" className="hover:text-text-primary">
            Help
          </Link>
          <a
            href={`mailto:${BRAND_CONTACT_EMAIL}`}
            className="hover:text-text-primary"
          >
            {BRAND_CONTACT_EMAIL}
          </a>
        </nav>
      </div>
    </footer>
  );
}
