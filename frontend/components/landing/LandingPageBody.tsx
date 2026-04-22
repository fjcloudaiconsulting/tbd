"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/components/auth/AuthProvider";
import TopNav from "@/components/landing/TopNav";
import Hero from "@/components/landing/Hero";
import FeatureTiles from "@/components/landing/FeatureTiles";
import SecondCta from "@/components/landing/SecondCta";
import LandingFooter from "@/components/landing/LandingFooter";

export default function LandingPageBody() {
  const { user, loading, needsSetup } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!loading) {
      if (needsSetup) {
        router.replace("/setup");
      } else if (user) {
        router.replace("/dashboard");
      }
    }
  }, [user, loading, needsSetup, router]);

  return (
    <div className="min-h-screen bg-bg text-text-primary">
      <TopNav />
      <main>
        <Hero />
        <FeatureTiles />
        <SecondCta />
      </main>
      <LandingFooter />
    </div>
  );
}
