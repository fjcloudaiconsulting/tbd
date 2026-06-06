import type { Metadata } from "next";
import LoginPageBody from "@/components/auth/LoginPageBody";
import { pageSocialMeta, siteName } from "@/lib/site";

const description = "Sign in to your The Better Decision account.";

export const metadata: Metadata = {
  title: "Sign in",
  description,
  alternates: {
    canonical: "/login",
  },
  ...pageSocialMeta({
    title: `Sign in · ${siteName}`,
    description,
    path: "/login",
  }),
  // A bare sign-in form has no search value and would only dilute the
  // index. Keep it crawlable (follow) so the noindex is seen, but out of
  // the index. /register stays indexable as a signup entry point.
  robots: { index: false, follow: true },
};

export default function LoginPage() {
  return <LoginPageBody />;
}
