import type { Metadata } from "next";
import OnboardingPageBody from "@/components/onboarding/OnboardingPageBody";

export const metadata: Metadata = {
  title: "Welcome",
  robots: { index: false, follow: false },
};

export default function OnboardingPage() {
  return <OnboardingPageBody />;
}
