import type { Metadata } from "next";
import LandingPageBody from "@/components/landing/LandingPageBody";

export const metadata: Metadata = {
  title: "The Better Decision — know your money, plan what's next",
  description:
    "A finance app for normal people. Know what you have, what's coming, and where it goes — without the spreadsheet fatigue.",
  openGraph: {
    title: "The Better Decision — know your money, plan what's next",
    description:
      "A finance app for normal people. Know what you have, what's coming, and where it goes.",
    type: "website",
    siteName: "The Better Decision",
  },
};

export default function LandingPage() {
  return <LandingPageBody />;
}
