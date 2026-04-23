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
};

export default function LoginPage() {
  return <LoginPageBody />;
}
