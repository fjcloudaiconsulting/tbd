import Link from "next/link";
import { btnPrimary } from "@/lib/styles";
import ThemeToggle from "@/components/ui/ThemeToggle";

export default function TopNav() {
  return (
    <nav className="flex items-center justify-between px-6 py-5 lg:px-10">
      <Link
        href="/"
        className="font-display text-lg font-semibold text-text-primary hover:opacity-80"
      >
        The Better Decision
      </Link>
      <div className="flex items-center gap-4">
        <Link
          href="/docs"
          className="text-sm text-text-muted transition-colors hover:text-text-primary"
        >
          Docs
        </Link>
        <Link
          href="/login"
          className="text-sm text-text-muted transition-colors hover:text-text-primary"
        >
          Sign in
        </Link>
        <Link
          href="/register"
          className={btnPrimary}
        >
          Get started
        </Link>
        <ThemeToggle />
      </div>
    </nav>
  );
}
