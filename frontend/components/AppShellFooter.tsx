import Link from "next/link";
import { Logo } from "@/components/brand/Logo";
import FeedbackTrigger from "@/components/feedback/FeedbackTrigger";
import CurrentYear from "@/components/ui/CurrentYear";
import { BRAND_CONTACT_EMAIL } from "@/lib/brand";

// Authed-shell footer (L5.4). Deliberately lighter scope than
// LandingFooter: users are already inside the app, so we keep this to
// legal + help + contact. No marketing nav (About, Pricing) here; the
// landing footer carries that load.
//
// Layout:
//   muted brand lockup + copyright on the left, link row on the right.
//   Stacks vertically on mobile (<sm) so the link row stays readable.
//
// Separators between the inline links use the middle dot "·" per
// BRAND.md voice rules (no em-dashes in customer copy).
export default function AppShellFooter() {
  return (
    <footer className="border-t border-border bg-surface px-4 sm:px-8 py-4">
      <div className="mx-auto flex max-w-[1760px] flex-col gap-2 text-[11px] text-text-muted sm:flex-row sm:items-center sm:justify-between sm:text-xs">
        <div className="flex items-center gap-3">
          <Logo tone="muted" size="sm" />
          <span className="inline-flex items-center gap-1">
            <span aria-hidden>&copy;</span>
            <CurrentYear />
          </span>
        </div>
        <nav
          aria-label="App footer"
          className="flex flex-wrap items-center gap-x-2 gap-y-1"
        >
          <Link href="/privacy" className="hover:text-text-primary">
            Privacy
          </Link>
          <span aria-hidden className="text-text-muted/60">
            &middot;
          </span>
          <Link href="/terms" className="hover:text-text-primary">
            Terms
          </Link>
          <span aria-hidden className="text-text-muted/60">
            &middot;
          </span>
          {/* /docs is the public in-app user manual (PR #159); reuses
              the landing footer convention so the Help label stays
              consistent across surfaces. */}
          <Link href="/docs" className="hover:text-text-primary">
            Help
          </Link>
          <span aria-hidden className="text-text-muted/60">
            &middot;
          </span>
          {/* In-app feedback widget trigger. Logged-in only — the
              component returns null when there is no authed user. */}
          <FeedbackTrigger />
          <span aria-hidden className="text-text-muted/60">
            &middot;
          </span>
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
