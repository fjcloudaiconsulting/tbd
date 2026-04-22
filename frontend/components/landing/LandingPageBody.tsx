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

  // Render a blank shell while auth state resolves OR while we're about to
  // redirect (logged-in user / needs-setup install). Prevents the public
  // landing from flashing before router.replace() fires. Anonymous visitors
  // hit the return below immediately since loading === false and both user
  // and needsSetup are null.
  if (loading || user || needsSetup) {
    return <div className="min-h-screen bg-bg" aria-busy="true" />;
  }

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
