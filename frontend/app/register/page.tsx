import type { Metadata } from "next";
import RegisterPageBody from "@/components/auth/RegisterPageBody";
import { readNonce } from "@/lib/nonce";
import { pageSocialMeta, siteName } from "@/lib/site";

// Trial copy hidden pending payment platform launch. Restore the
// "14-day free trial, no credit card required." sentence when
// BILLING_UI_ENABLED flips to true (Option A from
// specs/2026-05-21-hide-billing-ui-until-payment.md).
const description =
  "Create your free account and start making better decisions with your money.";

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
  robots: { index: true, follow: true },
};

export default async function RegisterPage() {
  // The Turnstile widget script must carry the per-request CSP nonce
  // (strict-dynamic CSP rejects nonceless scripts in production). Read
  // it from the proxy-injected ``x-nonce`` header and pass to the
  // client component, which forwards it to ``<Turnstile scriptOptions>``.
  const nonce = await readNonce();
  return <RegisterPageBody cspNonce={nonce} />;
}
