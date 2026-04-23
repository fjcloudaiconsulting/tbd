import type { Metadata } from "next";
import RegisterPageBody from "@/components/auth/RegisterPageBody";

export const metadata: Metadata = {
  title: "Create your account",
  description:
    "Create your free account and start making better decisions with your money. 14-day free trial, no credit card required.",
  alternates: {
    canonical: "/register",
  },
};

export default function RegisterPage() {
  return <RegisterPageBody />;
}
