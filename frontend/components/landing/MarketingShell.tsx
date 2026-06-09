import type { ReactNode } from "react";
import TopNav from "./TopNav";
import LandingFooter from "./LandingFooter";

// Shared marketing chrome for the public secondary pages (/features, /compare,
// /vs/*), mirroring the homepage shell so search/AI visitors never land on an
// orphaned page. Server component; safe for the apex static export (TopNav and
// LandingFooter already ship in the homepage apex export).
export default function MarketingShell({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-screen bg-bg text-text-primary">
      <TopNav />
      {children}
      <LandingFooter />
    </div>
  );
}
