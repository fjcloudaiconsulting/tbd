import type { Metadata } from "next";
import { Suspense } from "react";
import SignupCompleteBody from "@/components/auth/SignupCompleteBody";

export const metadata: Metadata = {
  title: "Finish signing up",
  // Reached only from the emailed link (TBD-654).
  robots: { index: false, follow: false },
};

export default function SignupCompletePage() {
  return (
    <Suspense fallback={null}>
      <SignupCompleteBody />
    </Suspense>
  );
}
