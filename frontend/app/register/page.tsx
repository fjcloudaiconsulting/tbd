import type { Metadata } from "next";
import RegisterPageBody from "@/components/auth/RegisterPageBody";
import { pageSocialMeta, siteName } from "@/lib/site";

const description =
  "Create your free account and start making better decisions with your money. 14-day free trial, no credit card required.";

export const metadata: Metadata = {
  title: "Create your account",
  description,
  alternates: {
    canonical: "/register",
  },
  ...pageSocialMeta({
    title: `Create your account · ${siteName}`,
    description,
    path: "/register",
  }),
};

export default function RegisterPage() {
  return <RegisterPageBody />;
}
