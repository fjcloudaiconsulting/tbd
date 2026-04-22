import Link from "next/link";

export default function LandingFooter() {
  const year = new Date().getFullYear();
  return (
    <footer className="border-t border-border">
      <div className="mx-auto flex max-w-6xl flex-col gap-4 px-6 py-8 text-xs text-text-muted lg:flex-row lg:items-center lg:justify-between lg:px-10">
        <div>
          © {year} The Better Decision
        </div>
        <div className="flex flex-wrap items-center gap-5">
          <Link href="/privacy" className="hover:text-text-primary">
            Privacy
          </Link>
          <Link href="/terms" className="hover:text-text-primary">
            Terms
          </Link>
          {/* TODO: /help 404s until roadmap L5.3 ships. Left linked intentionally so we don't have to edit the footer again; acceptable short-lived gap. */}
          <Link href="/help" className="hover:text-text-primary">
            Help
          </Link>
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
