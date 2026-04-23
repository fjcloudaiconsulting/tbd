import Link from "next/link";
import CurrentYear from "@/components/ui/CurrentYear";

export default function LandingFooter() {
  return (
    <footer className="border-t border-border">
      <div className="mx-auto flex max-w-6xl flex-col gap-4 px-6 py-8 text-xs text-text-muted lg:flex-row lg:items-center lg:justify-between lg:px-10">
        <div>
          © <CurrentYear /> The Better Decision
        </div>
        <div className="flex flex-wrap items-center gap-5">
          <Link href="/privacy" className="hover:text-text-primary">
            Privacy
          </Link>
          <Link href="/terms" className="hover:text-text-primary">
            Terms
          </Link>
          {/* Help link intentionally omitted until roadmap L5.3 ships a
              /help page. Visitors with questions can use the contact
              email below in the meantime. */}
          <a
            href="mailto:hello@thebetterdecision.com"
            className="hover:text-text-primary"
          >
            hello@thebetterdecision.com
          </a>
        </div>
      </div>
    </footer>
  );
}
