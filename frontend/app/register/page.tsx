import type { Metadata } from "next";
import RegisterPageBody from "@/components/auth/RegisterPageBody";
import { readNonce } from "@/lib/nonce";
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

export default async function RegisterPage() {
  // The Turnstile widget script must carry the per-request CSP nonce
  // (strict-dynamic CSP rejects nonceless scripts in production). Read
  // it from the proxy-injected ``x-nonce`` header and pass to the
  // client component, which forwards it to ``<Turnstile scriptOptions>``.
  const nonce = await readNonce();
  return <RegisterPageBody cspNonce={nonce} />;
}
