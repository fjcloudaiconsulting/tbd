import type { Metadata } from "next";
import LoginPageBody from "@/components/auth/LoginPageBody";

export const metadata: Metadata = {
  title: "Sign in",
  description: "Sign in to your The Better Decision account.",
  alternates: {
    canonical: "/login",
  },
};

export default function LoginPage() {
  return <LoginPageBody />;
}
