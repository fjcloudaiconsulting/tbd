// AuthProviderApex.tsx — no-op AuthProvider stub used by the apex
// (S3 + CloudFront) build target. The apex host serves only landing pages
// and never speaks to the backend, so we drop the real AuthProvider's
// /me probe + token refresh logic from the bundle. Aliased in by
// next.config.apex.ts.
//
// Re-exports the same named members the real module exports so any landing
// component that pulls in useAuth still type-checks during the apex build,
// even though no apex page actually invokes it (the LandingAuthRedirect
// island is also aliased to a no-op stub).

export class MfaRequiredError extends Error {
  constructor(public mfaToken: string) {
    super("MFA required (apex stub)");
    this.name = "MfaRequiredError";
  }
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  return <>{children}</>;
}

export function useAuth() {
  return {
    user: null,
    loading: false,
    needsSetup: false,
    async login(): Promise<void> {
      throw new Error("Apex build cannot perform auth flows.");
    },
    async register(): Promise<void> {
      throw new Error("Apex build cannot perform auth flows.");
    },
    async logout(): Promise<void> {
      // no-op in apex
    },
    async refresh(): Promise<void> {
      // no-op in apex
    },
  };
}
