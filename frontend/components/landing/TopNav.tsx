import Link from "next/link";
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
          href="/login"
          className="text-sm text-text-muted transition-colors hover:text-text-primary"
        >
          Sign in
        </Link>
        <Link
          href="/register"
          className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-accent-text hover:bg-accent-hover"
        >
          Get started
        </Link>
        <ThemeToggle />
      </div>
    </nav>
  );
}
