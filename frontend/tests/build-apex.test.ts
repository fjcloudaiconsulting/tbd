import { afterEach, describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import path from "node:path";

// build-apex.test.ts — static assertions about the apex build target
// that do not require actually running `next build` in the test process.
//
// What's covered here without a real build:
//   - Allowlist contents of scripts/build-apex.sh (vs app/ on disk).
//   - next.config.apex.ts exports `output: 'export'` and the auth-island
//     + AuthProvider webpack aliases.
//   - package.json has the build:apex script wired to the bash driver.
//   - lib/links.ts returns relative paths when NEXT_PUBLIC_BUILD_TARGET
//     is unset and absolute BRAND_APP_URL paths when set to "apex".
//
// We intentionally do NOT shell out to `next build` here — it takes ~20s
// and the integration is owned by CI.

const frontendDir = path.resolve(__dirname, "..");

function readText(rel: string): string {
  return readFileSync(path.join(frontendDir, rel), "utf-8");
}

describe("apex build target — scripts/build-apex.sh", () => {
  const script = readText("scripts/build-apex.sh");

  it("uses strict bash flags (set -euo pipefail)", () => {
    expect(script).toContain("set -euo pipefail");
  });

  it("traps EXIT / INT / TERM to restore the app/ tree", () => {
    expect(script).toMatch(/trap\s+restore_routes\s+EXIT\s+INT\s+TERM/);
  });

  it("invokes next build with BUILD_TARGET=apex env", () => {
    expect(script).toContain("NEXT_PUBLIC_BUILD_TARGET=apex");
    expect(script).toMatch(/npx\s+next\s+build/);
  });

  it("swaps next.config.ts <-> next.config.apex.ts (Next 16 has no --config flag)", () => {
    expect(script).toContain("next.config.apex.ts");
    expect(script).toContain("next.config.ts");
    // Restoration path must include the backup move.
    expect(script).toContain("CONFIG_BACKUP");
  });

  it("emits _meta.json with commit + build_at + target=apex", () => {
    expect(script).toContain("_meta.json");
    expect(script).toContain('"target": "apex"');
    expect(script).toMatch(/git[^\n]+rev-parse HEAD/);
  });

  it("excludes every authed / app route directory from the apex build", () => {
    // Sample of the routes that must NOT ship to apex. Each must appear
    // in the EXCLUDED_ROUTE_DIRS array of the build script.
    const mustExclude = [
      "dashboard",
      "transactions",
      "accounts",
      "admin",
      "login",
      "register",
      "settings",
      "setup",
      "profile",
      "verify-email",
      "forgot-password",
      "reset-password",
      "mfa-verify",
      "accept-invite",
      "budgets",
      "categories",
      "forecast-plans",
      "recurring",
      "import",
      "system",
      "auth",
      "health",
    ];
    for (const dir of mustExclude) {
      expect(script, `expected excluded route: ${dir}`).toContain(`"${dir}"`);
    }
  });

  it("allowlists the apex-exported routes only", () => {
    // Output sanity-prune must permit privacy, terms, docs + the
    // structural assets, and nothing else.
    for (const allowed of [
      "index.html",
      "privacy",
      "terms",
      "docs",
      "_next",
      "_meta.json",
    ]) {
      expect(script).toContain(`"${allowed}"`);
    }
  });
});

describe("apex build target — next.config.apex.ts", () => {
  const config = readText("next.config.apex.ts");

  it("sets output: 'export' (static export)", () => {
    expect(config).toMatch(/output:\s*["']export["']/);
  });

  it("sets trailingSlash: true and unoptimized images for S3 / CloudFront", () => {
    expect(config).toMatch(/trailingSlash:\s*true/);
    expect(config).toMatch(/unoptimized:\s*true/);
  });

  it("does NOT set a headers() block (CloudFront owns response headers)", () => {
    // The standalone next.config.ts wires CSP + HSTS etc. via headers().
    // `output: 'export'` ignores that contract; we must not silently set
    // it here to avoid the false impression that it does anything.
    expect(config).not.toMatch(/async\s+headers\s*\(/);
  });

  it("aliases the auth-island to the no-op apex stub", () => {
    expect(config).toContain("@/components/landing/LandingAuthRedirect");
    expect(config).toContain("LandingAuthRedirectApex.tsx");
  });

  it("aliases AuthProvider to the no-op apex stub", () => {
    expect(config).toContain("@/components/auth/AuthProvider");
    expect(config).toContain("AuthProviderApex.tsx");
  });

  it("writes to a separate distDir so the standard build is undisturbed", () => {
    expect(config).toMatch(/distDir:\s*["']\.next-apex["']/);
  });
});

describe("apex build target — package.json scripts", () => {
  const pkg = JSON.parse(readText("package.json")) as {
    scripts: Record<string, string>;
  };

  it("exposes build:apex that delegates to the bash driver", () => {
    expect(pkg.scripts["build:apex"]).toBeDefined();
    expect(pkg.scripts["build:apex"]).toContain("scripts/build-apex.sh");
  });

  it("exposes start:apex-preview that serves out-apex on a known port", () => {
    expect(pkg.scripts["start:apex-preview"]).toBeDefined();
    expect(pkg.scripts["start:apex-preview"]).toContain("out-apex");
  });

  it("keeps the standard build / start scripts intact", () => {
    expect(pkg.scripts.build).toBe("next build");
    expect(pkg.scripts.start).toBe("next start");
  });
});

describe("apex build target — apex stubs", () => {
  it("LandingAuthRedirectApex exists and is a default-exported component", () => {
    const stub = readText("components/landing/LandingAuthRedirectApex.tsx");
    expect(stub).toContain("export default function LandingAuthRedirectApex");
    // No useAuth, no useRouter — must be auth-free.
    expect(stub).not.toContain("useAuth");
    expect(stub).not.toContain("useRouter");
  });

  it("AuthProviderApex exposes useAuth + AuthProvider + MfaRequiredError", () => {
    const stub = readText("components/auth/AuthProviderApex.tsx");
    expect(stub).toContain("export function AuthProvider");
    expect(stub).toContain("export function useAuth");
    expect(stub).toContain("MfaRequiredError");
    // The whole point of this stub is to NOT call apiFetch.
    expect(stub).not.toContain("apiFetch");
  });
});

// lib/links.ts reads NEXT_PUBLIC_BUILD_TARGET at module evaluation time.
// vi.resetModules() forces a fresh evaluation per test case so each env
// permutation is independent.
async function loadLinks(env: { target?: string; appUrl?: string }) {
  delete process.env.NEXT_PUBLIC_BUILD_TARGET;
  delete process.env.NEXT_PUBLIC_APP_URL;
  if (env.target !== undefined) {
    process.env.NEXT_PUBLIC_BUILD_TARGET = env.target;
  }
  if (env.appUrl !== undefined) {
    process.env.NEXT_PUBLIC_APP_URL = env.appUrl;
  }
  vi.resetModules();
  return (await import("@/lib/links")) as typeof import("@/lib/links");
}

describe("apex build target — lib/links.ts cross-domain CTAs", () => {
  const origTarget = process.env.NEXT_PUBLIC_BUILD_TARGET;
  const origUrl = process.env.NEXT_PUBLIC_APP_URL;

  afterEach(() => {
    if (origTarget === undefined) {
      delete process.env.NEXT_PUBLIC_BUILD_TARGET;
    } else {
      process.env.NEXT_PUBLIC_BUILD_TARGET = origTarget;
    }
    if (origUrl === undefined) {
      delete process.env.NEXT_PUBLIC_APP_URL;
    } else {
      process.env.NEXT_PUBLIC_APP_URL = origUrl;
    }
    vi.resetModules();
  });

  it("returns relative paths in the standard app build target", async () => {
    const mod = await loadLinks({});
    expect(mod.signinHref()).toBe("/login");
    expect(mod.signupHref()).toBe("/register");
    expect(mod.IS_APEX_BUILD).toBe(false);
  });

  it("returns absolute BRAND_APP_URL paths in the apex build target", async () => {
    const mod = await loadLinks({ target: "apex" });
    expect(mod.signinHref()).toBe("https://app.thebetterdecision.com/login");
    expect(mod.signupHref()).toBe("https://app.thebetterdecision.com/register");
    expect(mod.IS_APEX_BUILD).toBe(true);
    expect(mod.BRAND_APP_URL).toBe("https://app.thebetterdecision.com");
  });

  it("honours NEXT_PUBLIC_APP_URL override and strips trailing slash", async () => {
    const mod = await loadLinks({ target: "apex", appUrl: "https://staging.example.com/" });
    expect(mod.BRAND_APP_URL).toBe("https://staging.example.com");
    expect(mod.signupHref()).toBe("https://staging.example.com/register");
  });

  it("ignores empty NEXT_PUBLIC_APP_URL and falls back to the default", async () => {
    const mod = await loadLinks({ target: "apex", appUrl: "" });
    expect(mod.BRAND_APP_URL).toBe("https://app.thebetterdecision.com");
  });
});
